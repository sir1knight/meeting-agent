#!/bin/bash
# Собирает build/Meeting Agent.app — самодостаточное приложение: внутри свой Python
# со всеми пакетами, код обработки и интерфейс. Модели скачиваются при первом запуске
# в ~/Library/Application Support/Meeting Agent.
#
#   ./build_app.sh            — собрать полное приложение (Python и пакеты внутри, ~1,6 ГБ — для сетей без PyPI)
#   ./build_app.sh --thin     — тонкое приложение (~10 МБ): Python и пакеты ставятся при первом запуске
#                               в папку данных и дальше обновляются кнопкой «Обновить»; → build/Meeting-Agent-Mac.zip
#   ./build_app.sh --install  — собрать полное и заменить копию в /Applications
#
# Пакеты Python берутся из .venv проекта (если версия Python совпадает) — без интернета.
# Иначе ставятся через pip (нужен интернет, ~1,5 ГБ).
set -euo pipefail
cd "$(dirname "$0")"
python3 scripts/sync_version.py   # версия и «что нового» из CHANGELOG.md → version.json
# Хеш списка пакетов Mac: приложение с Python вне .app доставит пакеты, если список поменялся.
python3 - "$(shasum -a 256 requirements-mac.lock | cut -c1-16)" <<'PY'
import json, sys
m = json.load(open("version.json"))
if m.setdefault("packages", {}).get("mac_hash") != sys.argv[1]:
    m["packages"]["mac_hash"] = sys.argv[1]
    m["packages"]["mac"] = "requirements-mac.lock"
    json.dump(m, open("version.json", "w"), ensure_ascii=False, indent=2); open("version.json", "a").write("\n")
PY

PY_TAG="20261021"
PY_VER="3.12.15"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/cpython-${PY_VER}%2B${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz"
APP="build/Meeting Agent.app"
RES="${APP}/Contents/Resources"
CACHE="build/cache"

say() { printf "\n\033[1m▸ %s\033[0m\n" "$1"; }
THIN=0; [[ "${1:-}" == "--thin" ]] && THIN=1
# Зеркало Python на GitLab (GitHub из некоторых сетей недоступен); проверяется по SHA-256 из runtime.json.
PY_MIRROR="https://gitlab.com/api/v4/projects/sir1knight%2FMeeting_agent/packages/generic/python/${PY_VER}-${PY_TAG}/cpython-${PY_VER}-${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz"

[[ "$(uname -m)" == "arm64" ]] || { echo "Нужен Mac на Apple Silicon"; exit 1; }
mkdir -p "${CACHE}"

say "Python ${PY_VER} (python-build-standalone ${PY_TAG})"
if [[ ! -s "${CACHE}/python.tar.gz" ]]; then
  curl -fL --retry 5 -C - -o "${CACHE}/python.tar.gz.part" "${PY_URL}"
  mv "${CACHE}/python.tar.gz.part" "${CACHE}/python.tar.gz"
fi

# Готовое окружение кэшируется и пересобирается, только если поменялись зависимости.
REQ_HASH=$(cat requirements.txt requirements-diarization.txt | shasum | cut -c1-12)
ENV="${CACHE}/python-${PY_VER}-${REQ_HASH}"
if [[ ${THIN} == 0 && ! -x "${ENV}/bin/python3" ]]; then
  say "Готовлю окружение Python с пакетами"
  rm -rf "${CACHE}"/python-"${PY_VER}"-* "${CACHE}/extract"
  mkdir -p "${CACHE}/extract"
  tar xzf "${CACHE}/python.tar.gz" -C "${CACHE}/extract"
  mv "${CACHE}/extract/python" "${ENV}"
  rm -rf "${CACHE}/extract"
  SITE="${ENV}/lib/python3.12/site-packages"
  VENV_SITE=".venv/lib/python3.12/site-packages"
  if [[ -d "${VENV_SITE}" && "$(.venv/bin/python -c 'import platform; print(platform.python_version())')" == "${PY_VER}" ]]; then
    echo "Копирую пакеты из .venv (та же версия Python)"
    cp -cR "${VENV_SITE}/" "${SITE}/"
    # Инструменты разработки внутри приложения не нужны.
    rm -rf "${SITE}"/pyflakes* "${SITE}"/pip "${SITE}"/pip-*
  else
    "${ENV}/bin/python3" -m pip install --upgrade pip
    "${ENV}/bin/python3" -m pip install -r requirements.txt -r requirements-diarization.txt
  fi
  # Лишнее из стандартной библиотеки: тесты, IDLE, Tk.
  rm -rf "${ENV}"/lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip} "${ENV}"/lib/{tcl,tk,itcl,thread}* 2>/dev/null || true
  find "${ENV}" -name "__pycache__" -type d -prune -exec rm -rf {} +
  # Проверка: всё нужное импортируется встроенным Python.
  PYTHONNOUSERSITE=1 "${ENV}/bin/python3" -I -c "import mlx_whisper, mlx_lm, soundfile, requests, anthropic, huggingface_hub, numpy; print('пакеты на месте')"
fi

say "Собираю приложение"
rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${RES}/app"
[[ ${THIN} == 0 ]] && cp -cR "${ENV}" "${RES}/python"
cp -R meeting_agent templates ui config.toml requirements-mac.lock "${RES}/app/"
# В приложение — version.json без ссылки на само приложение (её контрольная сумма известна только после сборки).
python3 -c "import json,sys; m=json.load(open('version.json')); m.pop('shell_download', None); json.dump(m, open(sys.argv[1], 'w'), ensure_ascii=False, indent=2)" "${RES}/app/version.json"
# Версия оболочки (Swift): shell_latest.mac — какая собирается; shell.mac — какая нужна коду (меньше или равна).
SHELL_VERSION=$(python3 -c "import json; m=json.load(open('version.json')); print((m.get('shell_latest') or m['shell'])['mac'])")
printf '{"shell": %s}\n' "${SHELL_VERSION}" > "${RES}/shell.json"
# Откуда при первом запуске скачать Python (тонкая сборка): зеркало на GitLab, затем GitHub.
PY_SHA=$(shasum -a 256 "${CACHE}/python.tar.gz" | cut -d' ' -f1)
printf '{"python": [{"url": "%s", "sha256": "%s"}, {"url": "%s", "sha256": "%s"}]}\n' \
  "${PY_MIRROR}" "${PY_SHA}" "${PY_URL}" "${PY_SHA}" > "${RES}/runtime.json"
find "${RES}/app" -name "__pycache__" -type d -prune -exec rm -rf {} +

say "Компилирую оболочку (Swift)"
swiftc -O -target arm64-apple-macos13.0 app/main.swift app/Backend.swift app/Recording.swift \
  app/Platforms.swift app/CalendarLookup.swift app/WebWindow.swift app/RecordingPill.swift app/RuntimeSetup.swift recorder/Recorder.swift -o "${APP}/Contents/MacOS/Meeting Agent" 2>&1 \
  | grep -v -E "installTap|DeprecatedDeclaration|^\s*[0-9]+ \||^\s*\||^\s*$|docs.swift.org" || true
[[ -x "${APP}/Contents/MacOS/Meeting Agent" ]] || { echo "Ошибка компиляции"; exit 1; }

say "Иконка"
if [[ ! -s "${CACHE}/AppIcon.icns" || app/make_icon.swift -nt "${CACHE}/AppIcon.icns" ]]; then
  ICONSET="${CACHE}/AppIcon.iconset"
  rm -rf "${ICONSET}" && mkdir -p "${ICONSET}"
  swift app/make_icon.swift "${CACHE}/icon-1024.png"
  for s in 16 32 128 256 512; do
    sips -z ${s} ${s} "${CACHE}/icon-1024.png" --out "${ICONSET}/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "${CACHE}/icon-1024.png" --out "${ICONSET}/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "${ICONSET}" -o "${CACHE}/AppIcon.icns"
fi
cp "${CACHE}/AppIcon.icns" "${RES}/AppIcon.icns"

VERSION="1.0.$(date +%Y%m%d%H%M)"
cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.meeting-agent</string>
  <key>CFBundleName</key><string>Meeting Agent</string>
  <key>CFBundleDisplayName</key><string>Meeting Agent</string>
  <key>CFBundleExecutable</key><string>Meeting Agent</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleDevelopmentRegion</key><string>ru</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Meeting Agent записывает ваш голос на встречах, чтобы составить протокол. Запись остаётся на этом Mac.</string>
  <key>NSCalendarsFullAccessUsageDescription</key>
  <string>Meeting Agent берёт из календаря название идущей встречи и список участников, чтобы точнее подписать собеседников. Календарь не меняется и никуда не отправляется.</string>
  <key>NSCalendarsUsageDescription</key>
  <string>Meeting Agent берёт из календаря название идущей встречи и список участников, чтобы точнее подписать собеседников. Календарь не меняется и никуда не отправляется.</string>
  <key>NSAudioCaptureUsageDescription</key>
  <string>Meeting Agent записывает звук собеседников на встречах без доступа к изображению экрана. Запись остаётся на этом Mac.</string>
  <key>NSScreenCaptureUsageDescription</key>
  <string>Meeting Agent использует доступ к экрану для определения заголовка окна Zoom и (на macOS 13–14.1) захвата системного звука. Изображение экрана не сохраняется.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# Постоянный сертификат «Meeting Agent Local» (создаётся один раз в Keychain Access): с ним macOS узнаёт
# приложение после любой пересборки и не сбрасывает разрешения на микрофон и запись экрана.
# Без него — одноразовая локальная подпись, и после каждой сборки разрешения придётся выдавать заново.
SIGN_ID="${SIGN_ID:-Meeting Agent Local}"
if security find-identity -v -p codesigning | grep -q "\"${SIGN_ID}\""; then
  say "Подписываю сертификатом «${SIGN_ID}»"
  SIGN="${SIGN_ID}"
else
  say "Подписываю (локальная подпись: разрешения macOS после пересборки придётся выдать заново)"
  SIGN="-"
fi
codesign --force --deep --timestamp=none --sign "${SIGN}" "${APP}" 2>&1 | grep -v "replacing existing signature" || true
codesign --verify "${APP}" && echo "Подпись в порядке"

du -sh "${APP}" | awk '{print "Размер: " $1}'

if [[ ${THIN} == 1 ]]; then
  rm -f build/Meeting-Agent-Mac.zip
  ditto -c -k --norsrc --noextattr --keepParent "${APP}" build/Meeting-Agent-Mac.zip
  du -h build/Meeting-Agent-Mac.zip | awk '{print "Архив: build/Meeting-Agent-Mac.zip, " $1}'
  shasum -a 256 build/Meeting-Agent-Mac.zip | awk '{print "SHA-256: " $1}'
elif [[ "${1:-}" == "--install" ]]; then
  say "Устанавливаю в /Applications"
  osascript -e 'quit app "Meeting Agent"' 2>/dev/null || true
  sleep 1
  rm -rf "/Applications/Meeting Agent.app"
  # Переносим, а не копируем: две копии с одним идентификатором путают macOS
  # (может открыться не та, а разрешения — привязаться не к той).
  mv "${APP}" /Applications/
  echo "Готово: /Applications/Meeting Agent.app"
else
  echo
  echo "Готово: ${APP} — перетащите его в «Программы»."
fi
