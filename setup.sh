#!/bin/bash
# Установка Meeting Agent на Mac с Apple Silicon. Повторный запуск безопасен.
set -euo pipefail
cd "$(dirname "$0")"

WHISPER_MODEL="mlx-community/whisper-large-v3-turbo"
LLM_MODEL="mlx-community/Qwen2.5-7B-Instruct-4bit"
DIAR_MODEL="pyannote/speaker-diarization-3.1"

say() { printf "\n\033[1m▸ %s\033[0m\n" "$1"; }

[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || { echo "Нужен Mac на Apple Silicon (M1 и новее)"; exit 1; }
command -v brew >/dev/null || { echo "Сначала установите Homebrew: https://brew.sh"; exit 1; }
xcode-select -p >/dev/null 2>&1 || { echo "Нужны Command Line Tools: выполните  xcode-select --install  и запустите setup.sh снова"; exit 1; }

say "Ставлю ffmpeg и Python 3.12"
brew install ffmpeg python@3.12

say "Создаю окружение Python"
PY="$(brew --prefix python@3.12)/bin/python3.12"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install -U pip
# Без -q: torch и mlx весят сотни мегабайт, пусть будет виден прогресс.
.venv/bin/pip install -r requirements.txt

say "Собираю рекордер (Swift)"
mkdir -p bin
swiftc -O recorder/Recorder.swift recorder/main.swift -o bin/zoomrec

say "Скачиваю модели с Hugging Face: распознавание речи (~1,6 ГБ) и языковую модель (~4,3 ГБ)"
for m in "$WHISPER_MODEL" "$LLM_MODEL"; do
  .venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('$m')"
done

echo
read -r -p "Установить разделение собеседников по голосам (pyannote, ~2 ГБ, нужен бесплатный токен Hugging Face)? [y/N] " ans
if [[ "$ans" =~ ^[YyДд] ]]; then
  echo "1) Зарегистрируйтесь на huggingface.co и создайте токен: https://huggingface.co/settings/tokens"
  echo "2) Примите условия моделей:"
  echo "   https://huggingface.co/pyannote/speaker-diarization-3.1"
  echo "   https://huggingface.co/pyannote/segmentation-3.0"
  if [[ ! -s .hf_token ]]; then
    read -r -s -p "Вставьте токен (ввод скрыт): " token; echo
    printf "%s" "$token" > .hf_token && chmod 600 .hf_token
  fi
  .venv/bin/pip install -r requirements-diarization.txt
  .venv/bin/python - <<EOF
from pyannote.audio import Pipeline
tok = open(".hf_token").read().strip()
try:
    p = Pipeline.from_pretrained("$DIAR_MODEL", token=tok)
except TypeError:
    p = Pipeline.from_pretrained("$DIAR_MODEL", use_auth_token=tok)
print("Модель разделения спикеров загружена" if p else "Не удалось загрузить: проверьте токен и принятые условия")
EOF
fi

chmod +x agent
mkdir -p ~/MeetingAgent/recordings ~/MeetingAgent/notes

say "Готово"
cat <<'EOF'
Дальше:
  1. Запустите  ./agent record --app all  и скажите пару фраз, затем Ctrl+C.
     macOS попросит разрешения «Микрофон» и «Запись экрана и системного звука» для Терминала:
     разрешите и перезапустите Терминал.
  2. Фоновый режим для Zoom:  ./agent watch
  Заметки появятся в ~/MeetingAgent/notes
EOF
