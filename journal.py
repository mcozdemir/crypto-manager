# -*- coding: utf-8 -*-
"""
journal.py
----------
Her taramada bulunan formasyon sinyallerini kalıcı bir veritabanına
kaydeder ve zaman içinde gerçekten hedefe mi yoksa stop'a mı gittiğini
takip eder. Böylece "Başarı %" tahminimizin gerçek dünyada ne kadar
isabetli olduğunu ölçmek mümkün olur.

Veritabanı: .env içinde Turso bilgileri varsa ortak bulut veritabanı
(Turso), yoksa proje klasöründeki signal_journal.db. Ayrıntılar: db.py
"""

import json
import logging
import os
import socket
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from version import VERSION, CHANGELOG, build_commit
from db import get_db, DatabaseError, PROJECT_DIR  # noqa: F401  (DatabaseError dışarıya da sunulur)

logger = logging.getLogger("scanner.journal")

# Aynı formasyonun her taramada tekrar tekrar kaydedilmesini önlemek için:
# aynı (symbol, timeframe, pattern, direction) kombinasyonu hâlâ "AÇIK"
# durumdaysa ve son kayıttan bu yana bu süre geçmediyse yeni satır eklenmez.
DEDUPE_WINDOW = timedelta(hours=6)

# Sinyali hangi bilgisayarın kaydettiği (birden fazla kişi aynı bulut
# veritabanını kullandığında ayırt etmek için).
SOURCE_NAME = socket.gethostname() or "bilinmiyor"

# Veritabanına ulaşılamadığında sinyallerin geçici olarak tutulduğu dosya.
PENDING_PATH = os.path.join(PROJECT_DIR, "pending_signals.jsonl")

_initialized = False

# Sonradan eklenen sütunlar (eski tablolara migration ile eklenir)
NEW_COLUMNS = [
    ("source", "TEXT"),
    ("market_score", "REAL"),       # Piyasa yönü (şimdi) uyum puanı, 0-100
    ("cm_score", "REAL"),           # Crypto Manager uyum puanı, 0-100
    ("fundamental_score", "REAL"),  # Temel analiz uyum puanı, 0-100
    ("app_version", "TEXT"),        # Sinyalin kaydedildiği uygulama sürümü (version.py)
    ("app_commit", "TEXT"),         # O sürümün git commit kısaltması (boşsa geriye dönük tahmin)
    ("hist_rate", "REAL"),          # Kayıt anındaki geçmiş başarı oranı (%)
    ("hist_n", "INTEGER"),          # Geçmiş başarı örnek sayısı
    ("rr", "REAL"),                 # Risk/ödül oranı
    ("confidence_json", "TEXT"),    # Tüm güven endeksi ayrıntıları (JSON)
]


def init_db(force: bool = False):
    """Tabloyu (yoksa) oluşturur; eski veritabanlarını göçürür (migration)."""
    global _initialized
    if _initialized and not force:
        return
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            pattern TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            target REAL NOT NULL,
            stop_loss REAL NOT NULL,
            score REAL,
            success_probability REAL,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'AÇIK',
            closed_at TEXT,
            close_price REAL,
            last_checked_at TEXT,
            last_price REAL,
            source TEXT,
            market_score REAL,
            cm_score REAL,
            hist_rate REAL,
            hist_n INTEGER,
            rr REAL,
            confidence_json TEXT,
            fundamental_score REAL,
            app_version TEXT,
            app_commit TEXT
        )
    """)
    # Göç (migration): sonradan eklenen sütunlar, eski tablolarda yoksa eklenir.
    existing_cols = {row["name"] for row in db.execute("PRAGMA table_info(signals)")}
    statements = []
    if "label" not in existing_cols:
        statements.append(("ALTER TABLE signals ADD COLUMN label TEXT", ()))
    for col, typ in NEW_COLUMNS:
        if col not in existing_cols:
            statements.append((f"ALTER TABLE signals ADD COLUMN {col} {typ}", ()))
    statements += [
        ("""CREATE INDEX IF NOT EXISTS idx_signals_lookup
            ON signals (symbol, timeframe, pattern, direction, status)""", ()),
        ("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals (created_at)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_signals_version ON signals (app_version)", ()),
        # Sürüm bilgisi eklenmeden önceki kayıtlar, içerdikleri verilere göre
        # geriye dönük etiketlenir (app_commit boş kalır = tahmini etiket).
        ("""UPDATE signals SET app_version='1.0.0'
            WHERE app_version IS NULL AND confidence_json IS NULL""", ()),
        ("""UPDATE signals SET app_version='1.2.0'
            WHERE app_version IS NULL AND confidence_json LIKE '%"fundamental": {%'""", ()),
        ("""UPDATE signals SET app_version='1.1.0'
            WHERE app_version IS NULL AND confidence_json IS NOT NULL""", ()),
        # Aynı sinyalin iki kez yazılmasını (ör. ağ hatası sonrası tekrar
        # deneme) veritabanı seviyesinde engeller.
        ("""CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique
            ON signals (symbol, timeframe, pattern, direction, created_at)""", ()),
    ]
    db.execute_many(statements)
    _initialized = True


INSERT_SQL = """
    INSERT OR IGNORE INTO signals
        (created_at, symbol, timeframe, pattern, direction, entry_price,
         target, stop_loss, score, success_probability, label, status,
         closed_at, close_price, last_checked_at, last_price, source,
         market_score, cm_score, hist_rate, hist_n, rr, confidence_json, fundamental_score,
         app_version, app_commit)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
INSERT_COLUMN_COUNT = 26


def _py(value):
    """numpy sayılarını (np.float64, np.int64...) düz Python değerine çevirir."""
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        return value.item()
    return value


def _load_pending() -> List[list]:
    """Daha önce veritabanına yazılamamış (ör. internet kesintisi) sinyaller."""
    if not os.path.exists(PENDING_PATH):
        return []
    rows = []
    try:
        with open(PENDING_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bekleyen sinyal dosyası okunamadı: %s", exc)
    return rows


def _save_pending(rows: List[list]) -> None:
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(list(row), ensure_ascii=False) + "\n")
    os.replace(tmp, PENDING_PATH)


def _dedupe(rows: List[list]) -> List[list]:
    """
    Aynı formasyon hâlâ 'AÇIK' ve DEDUPE_WINDOW içinde zaten kayıtlıysa
    satırı atar. Veritabanındaki açık sinyaller tek sorguda okunur.
    """
    last_open: Dict[tuple, datetime] = {}
    for row in get_db().execute("""
        SELECT symbol, timeframe, pattern, direction, MAX(created_at) AS last_created
        FROM signals WHERE status='AÇIK'
        GROUP BY symbol, timeframe, pattern, direction
    """):
        try:
            last_open[(row["symbol"], row["timeframe"], row["pattern"], row["direction"])] = \
                datetime.fromisoformat(row["last_created"])
        except Exception:  # noqa: BLE001
            pass

    kept = []
    for row in rows:
        created = datetime.fromisoformat(row[0])
        key = (row[1], row[2], row[3], row[4])
        last = last_open.get(key)
        if last is not None and abs(created - last) < DEDUPE_WINDOW:
            continue  # çok yakın zamanda zaten kaydedilmiş, atla
        last_open[key] = created  # aynı partideki tekrarları da engelle
        kept.append(row)
    return kept


def record_signals(results: List[Dict]) -> int:
    """
    Bir tarama sonucundaki formasyonları günlüğe kaydeder. Aynı formasyon
    hâlâ 'AÇIK' durumda ve DEDUPE_WINDOW içinde zaten kaydedilmişse
    tekrar eklenmez (spam önleme). Tüm yeni kayıtlar tek işlemde yazılır.

    Veritabanına ulaşılamazsa sinyaller kaybolmaz: pending_signals.jsonl
    dosyasına alınır ve bir sonraki başarılı kayıtta otomatik gönderilir.
    Döndürür: veritabanına eklenen sinyal sayısı.
    """
    now_iso = datetime.now().isoformat(timespec="seconds")
    new_rows = [[_py(v) for v in [
        now_iso, r["symbol"], r["timeframe"], r["pattern"], r["direction"],
        r["last_price"], r["target"], r["stop_loss"], r.get("score"),
        r.get("success_probability"), r.get("label"), "AÇIK",
        None, None, now_iso, r["last_price"], SOURCE_NAME,
        r.get("market_score"), r.get("cm_score"), r.get("hist_rate"), r.get("hist_n"),
        r.get("rr"), json.dumps(r["confidence"], ensure_ascii=False, default=_py)
        if r.get("confidence") else None,
        r.get("fundamental_score"),
        VERSION, build_commit(),  # boş metin = commit bilinmiyor; NULL = geriye dönük etiket
    ]] for r in results or []]
    # Eski sürümün bekleyen kayıtlarında yeni sütunlar yok: boş değerle tamamla
    pending = [row + [None] * (INSERT_COLUMN_COUNT - len(row)) for row in _load_pending()]
    if not new_rows and not pending:
        return 0

    try:
        init_db()
        rows = _dedupe(pending + new_rows)
        get_db().execute_many([(INSERT_SQL, row) for row in rows])
    except DatabaseError as exc:
        _save_pending(pending + new_rows)
        logger.warning("Veritabanına yazılamadı, %d sinyal bekleyen listeye alındı: %s",
                       len(pending) + len(new_rows), exc)
        raise

    if pending:
        os.remove(PENDING_PATH)
        logger.info("Daha önce bekleyen %d sinyal veritabanına gönderildi.", len(pending))
    logger.info("Günlüğe %d yeni sinyal eklendi (%s).", len(rows), get_db().describe())
    return len(rows)


def refresh_open_signals(price_lookup: Dict[str, float]) -> Dict[str, int]:
    """
    'AÇIK' durumdaki tüm sinyalleri, verilen güncel fiyatlara (symbol -> price)
    göre günceller. Fiyat hedefe ulaştıysa 'HEDEF', stop'a ulaştıysa 'STOP'
    olarak işaretlenir. Not: Bu kontrol yalnızca ANLIK fiyatı kullanır;
    iki tarama arasında hedef/stop'a değip geri dönen durumlar (intra-bar
    dokunuş) yakalanamayabilir. Daha kesin sonuç için taramaları sık
    aralıklarla çalıştırmanız önerilir.
    """
    init_db()
    db = get_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    stats = {"güncellenen": 0, "hedef": 0, "stop": 0}
    statements = []

    for row in get_open_signals():
        price = price_lookup.get(row["symbol"])
        if price is None:
            continue

        new_status = None
        if row["direction"] == "LONG":
            if price >= row["target"]:
                new_status = "HEDEF"
            elif price <= row["stop_loss"]:
                new_status = "STOP"
        else:
            if price <= row["target"]:
                new_status = "HEDEF"
            elif price >= row["stop_loss"]:
                new_status = "STOP"

        if new_status:
            # status='AÇIK' koşulu: başka bir bilgisayar aynı sinyali bu arada
            # kapattıysa onun sonucunun üzerine yazılmaz.
            statements.append(("""
                UPDATE signals SET status=?, closed_at=?, close_price=?,
                    last_checked_at=?, last_price=?
                WHERE id=? AND status='AÇIK'
            """, (new_status, now_iso, price, now_iso, price, row["id"])))
            stats["hedef" if new_status == "HEDEF" else "stop"] += 1
        else:
            statements.append(("""
                UPDATE signals SET last_checked_at=?, last_price=?
                WHERE id=? AND status='AÇIK'
            """, (now_iso, price, row["id"])))
        stats["güncellenen"] += 1

    db.execute_many(statements)
    logger.info("Sinyal günlüğü güncellendi: %s", stats)
    return stats


def get_signals(limit: int = 500, symbol: Optional[str] = None,
                status: Optional[str] = None, timeframe: Optional[str] = None,
                since: Optional[str] = None, until: Optional[str] = None,
                version: Optional[str] = None) -> List[Dict]:
    """
    Kayıtlı sinyalleri en yeniden eskiye döndürür. İsteğe bağlı filtreler:
      symbol    : ör. "BTCUSDT"
      status    : "AÇIK", "HEDEF" veya "STOP"
      timeframe : ör. "4h"
      since/until: ISO tarih (ör. "2026-09-01" veya "2026-09-01T12:00:00")
      version   : uygulama sürümü (ör. "1.3.0")
    """
    init_db()
    where, args = [], []
    if symbol:
        where.append("symbol = ?")
        args.append(symbol.upper())
    if status:
        where.append("status = ?")
        args.append(status)
    if timeframe:
        where.append("timeframe = ?")
        args.append(timeframe)
    if version:
        where.append("app_version = ?")
        args.append(version.lstrip("v"))
    if since:
        where.append("created_at >= ?")
        args.append(since)
    if until:
        where.append("created_at <= ?")
        args.append(until)
    sql = "SELECT * FROM signals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(int(limit))
    return [_with_confidence(r) for r in get_db().execute(sql, args)]


def _with_confidence(row: Dict) -> Dict:
    """confidence_json metnini arayüz için sözlüğe çevirir."""
    raw = row.pop("confidence_json", None)
    row["confidence"] = None
    if raw:
        try:
            row["confidence"] = json.loads(raw)
        except (TypeError, ValueError):
            pass
    return row


def get_all_signals(limit: int = 500) -> List[Dict]:
    return get_signals(limit=limit)


def get_open_signals() -> List[Dict]:
    """Tüm 'AÇIK' sinyaller (limit yok)."""
    init_db()
    return get_db().execute("SELECT * FROM signals WHERE status='AÇIK' ORDER BY created_at DESC")


def get_version_stats() -> Dict:
    """
    Sürüm bazında sinyal sonuçları ve endeks isabeti. "Endeks isabeti":
    ilgili endeks sinyali destekliyorken (puan >= 65) sonuçlanan
    sinyallerin hedef oranı — bir sürümün endeksi ne kadar işe yarıyor.
    """
    init_db()
    rows = get_db().execute("""
        SELECT COALESCE(app_version, '?') AS version,
               MIN(created_at) AS first_at, MAX(created_at) AS last_at,
               COUNT(*) AS total,
               SUM(CASE WHEN status='AÇIK'  THEN 1 ELSE 0 END) AS open,
               SUM(CASE WHEN status='HEDEF' THEN 1 ELSE 0 END) AS hedef,
               SUM(CASE WHEN status='STOP'  THEN 1 ELSE 0 END) AS stop,
               SUM(CASE WHEN status='HEDEF' AND cm_score >= 65 THEN 1 ELSE 0 END) AS cm_hit,
               SUM(CASE WHEN status IN ('HEDEF','STOP') AND cm_score >= 65 THEN 1 ELSE 0 END) AS cm_n,
               SUM(CASE WHEN status='HEDEF' AND market_score >= 65 THEN 1 ELSE 0 END) AS mk_hit,
               SUM(CASE WHEN status IN ('HEDEF','STOP') AND market_score >= 65 THEN 1 ELSE 0 END) AS mk_n,
               SUM(CASE WHEN status='HEDEF' AND fundamental_score >= 65 THEN 1 ELSE 0 END) AS fd_hit,
               SUM(CASE WHEN status IN ('HEDEF','STOP') AND fundamental_score >= 65 THEN 1 ELSE 0 END) AS fd_n,
               MAX(CASE WHEN app_commit IS NULL THEN 1 ELSE 0 END) AS inferred
        FROM signals GROUP BY COALESCE(app_version, '?')
    """)
    notes = {v: (d, t) for v, d, t in CHANGELOG}

    def rate(hit, n):
        n = int(n or 0)
        return {"rate": round(int(hit or 0) / n * 100, 1) if n else None, "n": n}

    stats = []
    for r in rows:
        resolved = int(r["hedef"] or 0) + int(r["stop"] or 0)
        stats.append({
            "version": r["version"],
            "released": notes.get(r["version"], (None, None))[0],
            "description": notes.get(r["version"], (None, None))[1],
            "first_at": r["first_at"], "last_at": r["last_at"],
            "total": int(r["total"] or 0), "open": int(r["open"] or 0),
            "hedef": int(r["hedef"] or 0), "stop": int(r["stop"] or 0),
            "win_rate": round(int(r["hedef"] or 0) / resolved * 100, 1) if resolved else None,
            "cm": rate(r["cm_hit"], r["cm_n"]),
            "market": rate(r["mk_hit"], r["mk_n"]),
            "fundamental": rate(r["fd_hit"], r["fd_n"]),
            "inferred": bool(r["inferred"]),
        })

    def vkey(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (-1,)
    stats.sort(key=lambda x: vkey(x["version"]), reverse=True)
    return {
        "current": VERSION,
        "commit": build_commit(),
        "changelog": [{"version": v, "date": d, "description": t} for v, d, t in CHANGELOG],
        "stats": stats,
    }


def get_summary(version: Optional[str] = None) -> Dict:
    init_db()
    where, args = "", ()
    if version:
        where, args = " WHERE app_version = ?", (version.lstrip("v"),)
    row = get_db().execute("""
        SELECT COUNT(*) AS toplam,
               COALESCE(SUM(CASE WHEN status='AÇIK'  THEN 1 ELSE 0 END), 0) AS acik,
               COALESCE(SUM(CASE WHEN status='HEDEF' THEN 1 ELSE 0 END), 0) AS hedef,
               COALESCE(SUM(CASE WHEN status='STOP'  THEN 1 ELSE 0 END), 0) AS stop
        FROM signals""" + where, args)[0]
    hit_target, hit_stop = int(row["hedef"]), int(row["stop"])
    resolved = hit_target + hit_stop
    win_rate = round(hit_target / resolved * 100, 1) if resolved > 0 else None
    return {
        "toplam": int(row["toplam"]),
        "açık": int(row["acik"]),
        "hedef": hit_target,
        "stop": hit_stop,
        "çözümlenen": resolved,
        "gerçek_başarı_oranı": win_rate,
    }


def clear_journal():
    """Tüm sinyal günlüğünü temizler (geliştirme/test amaçlı)."""
    init_db()
    get_db().execute("DELETE FROM signals")
