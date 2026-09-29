# -*- coding: utf-8 -*-
"""
journal.py
----------
Her taramada bulunan formasyon sinyallerini kalıcı bir SQLite veritabanına
kaydeder ve zaman içinde gerçekten hedefe mi yoksa stop'a mı gittiğini
takip eder. Böylece "Başarı %" tahminimizin gerçek dünyada ne kadar
isabetli olduğunu ölçmek mümkün olur.

Veritabanı dosyası: signal_journal.db (proje klasöründe, çalışma anında
otomatik oluşturulur).
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger("scanner.journal")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_journal.db")

# Aynı formasyonun her taramada tekrar tekrar kaydedilmesini önlemek için:
# aynı (symbol, timeframe, pattern, direction) kombinasyonu hâlâ "AÇIK"
# durumdaysa ve son kayıttan bu yana bu süre geçmediyse yeni satır eklenmez.
DEDUPE_WINDOW = timedelta(hours=6)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Veritabanı ve tabloyu (yoksa) oluşturur; eski veritabanlarını göçürür (migration)."""
    with _connect() as conn:
        conn.execute("""
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
                last_price REAL
            )
        """)
        # Göç (migration): "label" sütunu eklenmeden önce oluşturulmuş eski
        # signal_journal.db dosyaları için sütunu sonradan ekle (varsa hata
        # vermeden atla). Böylece v1/v2/v3/v4 klasörlerinizdeki mevcut
        # veritabanları veri kaybı olmadan güncellenir.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
        if "label" not in existing_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN label TEXT")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_lookup
            ON signals (symbol, timeframe, pattern, direction, status)
        """)
        conn.commit()


def record_signals(results: List[Dict]) -> int:
    """
    Bir tarama sonucundaki formasyonları günlüğe kaydeder. Aynı formasyon
    hâlâ 'AÇIK' durumda ve DEDUPE_WINDOW içinde zaten kaydedilmişse
    tekrar eklenmez (spam önleme).
    Döndürür: yeni eklenen sinyal sayısı.
    """
    init_db()
    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    inserted = 0

    with _connect() as conn:
        for r in results:
            existing = conn.execute("""
                SELECT id, created_at FROM signals
                WHERE symbol=? AND timeframe=? AND pattern=? AND direction=? AND status='AÇIK'
                ORDER BY created_at DESC LIMIT 1
            """, (r["symbol"], r["timeframe"], r["pattern"], r["direction"])).fetchone()

            if existing:
                try:
                    last_created = datetime.fromisoformat(existing["created_at"])
                except Exception:  # noqa: BLE001
                    last_created = now
                if now - last_created < DEDUPE_WINDOW:
                    continue  # çok yakın zamanda zaten kaydedilmiş, atla

            conn.execute("""
                INSERT INTO signals
                    (created_at, symbol, timeframe, pattern, direction, entry_price,
                     target, stop_loss, score, success_probability, label, status,
                     last_checked_at, last_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AÇIK', ?, ?)
            """, (
                now_iso, r["symbol"], r["timeframe"], r["pattern"], r["direction"],
                r["last_price"], r["target"], r["stop_loss"], r.get("score"),
                r.get("success_probability"), r.get("label"), now_iso, r["last_price"],
            ))
            inserted += 1
        conn.commit()

    logger.info("Günlüğe %d yeni sinyal eklendi.", inserted)
    return inserted


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
    now_iso = datetime.now().isoformat(timespec="seconds")
    stats = {"güncellenen": 0, "hedef": 0, "stop": 0}

    with _connect() as conn:
        rows = conn.execute("SELECT * FROM signals WHERE status='AÇIK'").fetchall()
        for row in rows:
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
                conn.execute("""
                    UPDATE signals SET status=?, closed_at=?, close_price=?,
                        last_checked_at=?, last_price=?
                    WHERE id=?
                """, (new_status, now_iso, price, now_iso, price, row["id"]))
                stats["hedef" if new_status == "HEDEF" else "stop"] += 1
            else:
                conn.execute("""
                    UPDATE signals SET last_checked_at=?, last_price=? WHERE id=?
                """, (now_iso, price, row["id"]))
            stats["güncellenen"] += 1
        conn.commit()

    logger.info("Sinyal günlüğü güncellendi: %s", stats)
    return stats


def get_all_signals(limit: int = 500) -> List[Dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM signals ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_summary() -> Dict:
    init_db()
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
        open_count = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='AÇIK'").fetchone()["c"]
        hit_target = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='HEDEF'").fetchone()["c"]
        hit_stop = conn.execute("SELECT COUNT(*) c FROM signals WHERE status='STOP'").fetchone()["c"]
        resolved = hit_target + hit_stop
        win_rate = round(hit_target / resolved * 100, 1) if resolved > 0 else None
        return {
            "toplam": total,
            "açık": open_count,
            "hedef": hit_target,
            "stop": hit_stop,
            "çözümlenen": resolved,
            "gerçek_başarı_oranı": win_rate,
        }


def clear_journal():
    """Tüm sinyal günlüğünü temizler (geliştirme/test amaçlı)."""
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM signals")
        conn.commit()
