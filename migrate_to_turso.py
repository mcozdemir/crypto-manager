# -*- coding: utf-8 -*-
"""
migrate_to_turso.py
-------------------
Yerel signal_journal.db dosyasındaki sinyal geçmişini .env içinde tanımlı
Turso bulut veritabanına aktarır.

Kullanım (proje klasöründe):
    .venv/bin/python migrate_to_turso.py

Tekrar çalıştırmak güvenlidir: zaten aktarılmış sinyaller (aynı sembol,
zaman dilimi, formasyon, yön ve kayıt zamanı) ikinci kez eklenmez.
Yerel dosya silinmez; yedek olarak kalır.
"""

import sqlite3
import sys

import db
import journal

COLUMNS = [
    "created_at", "symbol", "timeframe", "pattern", "direction", "entry_price",
    "target", "stop_loss", "score", "success_probability", "label", "status",
    "closed_at", "close_price", "last_checked_at", "last_price", "source",
]
BATCH_SIZE = 50


def main() -> int:
    target = db.get_db()
    if target.kind != "turso":
        print(f"HATA: .env içinde {db.ENV_URL_KEY} ve {db.ENV_TOKEN_KEY} tanımlı değil.")
        return 1

    conn = sqlite3.connect(db.LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    local_cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
    rows = conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
    conn.close()
    print(f"Yerel veritabanında {len(rows)} sinyal bulundu.")

    journal.init_db()
    before = journal.get_summary()["toplam"]

    statements = []
    for row in rows:
        values = [row[c] if c in local_cols else None for c in COLUMNS]
        if values[COLUMNS.index("source")] is None:
            values[COLUMNS.index("source")] = "yerel-aktarim"
        statements.append((journal.INSERT_SQL, values))

    for i in range(0, len(statements), BATCH_SIZE):
        target.execute_many(statements[i:i + BATCH_SIZE])
        print(f"  {min(i + BATCH_SIZE, len(statements))}/{len(statements)} gönderildi")

    summary = journal.get_summary()
    print(f"Aktarım tamamlandı: {summary['toplam'] - before} yeni sinyal eklendi.")
    print(f"Turso'daki durum: toplam {summary['toplam']}, açık {summary['açık']}, "
          f"hedef {summary['hedef']}, stop {summary['stop']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
