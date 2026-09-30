#!/bin/zsh

set -e

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Kurulum bulunamadı. Lütfen bu klasörde kurulumu yeniden çalıştırın."
  read -r "?Kapatmak için Enter'a basın..."
  exit 1
fi

# requirements.txt değiştiyse eksik paketleri otomatik kur
if [[ requirements.txt -nt .venv/.requirements-stamp ]]; then
  echo "Gerekli paketler güncelleniyor..."
  .venv/bin/python -m pip install -q -r requirements.txt && touch .venv/.requirements-stamp
fi

export MPLCONFIGDIR="$PROJECT_DIR/.matplotlib-cache"
mkdir -p "$MPLCONFIGDIR"

CONFIG_VALUES="$("$PROJECT_DIR/.venv/bin/python" -c 'from ayarlar import WEB_PORT, AUTO_OPEN_BROWSER; print(f"{WEB_PORT}|{int(AUTO_OPEN_BROWSER)}")')"
PORT="${SCANNER_PORT:-${CONFIG_VALUES%%|*}}"
AUTO_OPEN="${CONFIG_VALUES##*|}"

if [[ "$AUTO_OPEN" == "1" ]]; then
  (sleep 2; open "http://127.0.0.1:$PORT") &
fi

echo "Kripto Formasyon Tarayıcı başlatılıyor..."
echo "Panel: http://127.0.0.1:$PORT"
echo "Kapatmak için bu pencereyi kapatabilir veya Control+C tuşlarına basabilirsiniz."
echo

exec .venv/bin/python app.py
