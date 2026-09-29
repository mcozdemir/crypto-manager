# -*- coding: utf-8 -*-
"""Kullanıcı tarafından değiştirilebilen uygulama ayarları.

Bu dosyadaki değerleri herhangi bir metin düzenleyiciyle değiştirebilirsiniz.
Değişikliklerin etkili olması için çalışan uygulamayı kapatıp yeniden açın.
"""

# Web paneli ayarları. 5000 portu macOS Control Center ile çakışabildiği
# için varsayılan olarak 5050 kullanılır.
WEB_HOST = "127.0.0.1"
WEB_PORT = 5050
DEBUG_MODE = False
AUTO_OPEN_BROWSER = True

# Canlı tarama ayarları.
DEFAULT_TOP_N = 50
TIMEFRAMES = ["1h", "4h", "1d"]
MIN_SCORE = 60
KLINE_LIMIT = 500

# Backtest ayarları.
BACKTEST_MIN_SCORE = MIN_SCORE
BACKTEST_MIN_GAP = 15
