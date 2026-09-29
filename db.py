# -*- coding: utf-8 -*-
"""
db.py
-----
Sinyal günlüğünün veritabanı katmanı.

- Proje klasöründeki .env dosyasında (veya ortam değişkenlerinde)
  APP_TURSO_TECH_DB_URL ve APP_TURSO_TECH_TOKEN tanımlıysa bulut
  veritabanı (Turso / libSQL) kullanılır. Böylece farklı bilgisayarlarda
  çalışan herkes aynı sinyal geçmişini görür ve günceller.
- Tanımlı değilse proje klasöründeki yerel signal_journal.db (SQLite)
  dosyası kullanılır (çevrimdışı / geliştirme modu).

Ek paket gerektirmez: Turso'ya resmi HTTP API'si (Hrana over HTTP,
/v2/pipeline) üzerinden Python standart kütüphanesiyle bağlanılır.
"""

import base64
import json
import logging
import numbers
import os
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("scanner.db")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(PROJECT_DIR, ".env")
LOCAL_DB_PATH = os.path.join(PROJECT_DIR, "signal_journal.db")

ENV_URL_KEY = "APP_TURSO_TECH_DB_URL"
ENV_TOKEN_KEY = "APP_TURSO_TECH_TOKEN"

# (sql, parametreler) çifti
Statement = Tuple[str, Sequence[Any]]


class DatabaseError(Exception):
    """Veritabanı sorgusu başarısız olduğunda fırlatılır."""


# --------------------------------------------------------------------------
# .env okuma
# --------------------------------------------------------------------------
def load_env(path: str = ENV_FILE) -> None:
    """
    Basit .env okuyucu (python-dotenv gerektirmez). ANAHTAR=değer satırlarını
    okur; zaten tanımlı ortam değişkenlerinin üzerine YAZMAZ.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                os.environ.setdefault(key, value)


# --------------------------------------------------------------------------
# Yerel SQLite
# --------------------------------------------------------------------------
class LocalSQLite:
    kind = "local"

    def __init__(self, path: str = LOCAL_DB_PATH):
        self.path = path

    def describe(self) -> str:
        return f"yerel SQLite ({os.path.basename(self.path)})"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def execute(self, sql: str, args: Sequence[Any] = ()) -> List[Dict]:
        with closing(self._connect()) as conn:
            try:
                rows = [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
                conn.commit()
                return rows
            except sqlite3.Error as exc:
                raise DatabaseError(str(exc)) from exc

    def execute_many(self, statements: List[Statement]) -> None:
        """Tüm ifadeleri tek bir işlem (transaction) içinde çalıştırır."""
        if not statements:
            return
        with closing(self._connect()) as conn:
            try:
                with conn:  # hata olursa otomatik ROLLBACK
                    for sql, args in statements:
                        conn.execute(sql, tuple(args))
            except sqlite3.Error as exc:
                raise DatabaseError(str(exc)) from exc


# --------------------------------------------------------------------------
# Turso (libSQL) — HTTP API
# --------------------------------------------------------------------------
class TursoHTTP:
    kind = "turso"

    def __init__(self, url: str, token: str, timeout: float = 20.0, retries: int = 3):
        url = url.strip().rstrip("/")
        for prefix in ("libsql://", "wss://", "ws://"):
            if url.startswith(prefix):
                url = "https://" + url[len(prefix):]
                break
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self.base_url = url
        self.token = token.strip()
        self.timeout = timeout
        self.retries = max(1, retries)

    def describe(self) -> str:
        host = self.base_url.split("://", 1)[-1]
        return f"Turso bulut veritabanı ({host})"

    # --- değer dönüşümleri ------------------------------------------------
    @staticmethod
    def _encode(value: Any) -> Dict:
        if value is None:
            return {"type": "null"}
        if hasattr(value, "item") and not isinstance(value, (bytes, bytearray, str)):
            value = value.item()  # numpy sayıları -> Python sayısı
        if isinstance(value, bool):
            return {"type": "integer", "value": str(int(value))}
        if isinstance(value, numbers.Integral):
            return {"type": "integer", "value": str(int(value))}
        if isinstance(value, numbers.Real):
            return {"type": "float", "value": float(value)}
        if isinstance(value, (bytes, bytearray)):
            return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii")}
        return {"type": "text", "value": str(value)}

    @staticmethod
    def _decode(cell: Dict) -> Any:
        kind = cell.get("type")
        if kind == "null":
            return None
        if kind == "integer":
            return int(cell["value"])
        if kind == "float":
            return float(cell["value"])
        if kind == "blob":
            return base64.b64decode(cell.get("base64", ""))
        return cell.get("value")

    def _stmt(self, sql: str, args: Sequence[Any] = ()) -> Dict:
        stmt: Dict[str, Any] = {"sql": sql}
        if args:
            stmt["args"] = [self._encode(a) for a in args]
        return stmt

    @staticmethod
    def _rows(result: Dict) -> List[Dict]:
        cols = [c.get("name") for c in result.get("cols", [])]
        return [
            {col: TursoHTTP._decode(cell) for col, cell in zip(cols, row)}
            for row in result.get("rows", [])
        ]

    # --- HTTP ---------------------------------------------------------------
    def _pipeline(self, requests: List[Dict]) -> List[Dict]:
        body = json.dumps({"requests": requests}).encode("utf-8")
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(
                self.base_url + "/v2/pipeline",
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))["results"]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code in (401, 403):
                    raise DatabaseError(
                        f"Turso yetkilendirme hatası ({exc.code}). .env içindeki "
                        f"{ENV_TOKEN_KEY} değerini kontrol edin."
                    ) from exc
                if exc.code < 500 and exc.code != 429:
                    raise DatabaseError(f"Turso HTTP {exc.code}: {detail}") from exc
                last_exc = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_exc = exc
            if attempt < self.retries:
                wait = 1.5 * attempt
                logger.warning("Turso'ya ulaşılamadı (%s), %.1f sn sonra tekrar denenecek...", last_exc, wait)
                time.sleep(wait)
        raise DatabaseError(f"Turso'ya bağlanılamadı: {last_exc}")

    @staticmethod
    def _raise_if_error(res: Dict) -> Dict:
        if res.get("type") != "ok":
            err = res.get("error") or {}
            raise DatabaseError(err.get("message") or f"Turso hatası: {res}")
        return res["response"]

    # --- genel arayüz -------------------------------------------------------
    def execute(self, sql: str, args: Sequence[Any] = ()) -> List[Dict]:
        results = self._pipeline([
            {"type": "execute", "stmt": self._stmt(sql, args)},
            {"type": "close"},
        ])
        response = self._raise_if_error(results[0])
        return self._rows(response["result"])

    def execute_many(self, statements: List[Statement]) -> None:
        """
        Tüm ifadeleri tek HTTP isteğinde, tek bir işlem (transaction) içinde
        çalıştırır: biri hata verirse hiçbiri kaydedilmez (ROLLBACK).
        """
        if not statements:
            return
        steps: List[Dict] = [{"stmt": {"sql": "BEGIN"}}]
        for sql, args in statements:
            steps.append({
                "stmt": self._stmt(sql, args),
                "condition": {"type": "ok", "step": len(steps) - 1},
            })
        commit_idx = len(steps)
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": commit_idx - 1}})
        steps.append({
            "stmt": {"sql": "ROLLBACK"},
            "condition": {"type": "not", "cond": {"type": "ok", "step": commit_idx}},
        })

        results = self._pipeline([
            {"type": "batch", "batch": {"steps": steps}},
            {"type": "close"},
        ])
        batch_result = self._raise_if_error(results[0])["result"]
        for err in batch_result.get("step_errors", []):
            if err:
                raise DatabaseError(err.get("message") or str(err))
        step_results = batch_result.get("step_results", [])
        if len(step_results) <= commit_idx or step_results[commit_idx] is None:
            raise DatabaseError("Turso işlemi tamamlanamadı (COMMIT çalışmadı).")


# --------------------------------------------------------------------------
# Seçim
# --------------------------------------------------------------------------
_db = None


def get_db():
    """Yapılandırmaya göre Turso veya yerel SQLite bağlantısını döndürür (tekil)."""
    global _db
    if _db is None:
        load_env()
        url = os.environ.get(ENV_URL_KEY, "").strip()
        token = os.environ.get(ENV_TOKEN_KEY, "").strip()
        if url and token:
            _db = TursoHTTP(url, token)
        else:
            if url or token:
                logger.warning("%s ve %s birlikte tanımlanmalı; yerel veritabanı kullanılıyor.",
                               ENV_URL_KEY, ENV_TOKEN_KEY)
            _db = LocalSQLite()
        logger.info("Sinyal günlüğü veritabanı: %s", _db.describe())
    return _db
