#!/bin/bash
# Собирает build/Meeting Agent.app — самодостаточное приложение: внутри свой Python
# со всеми пакетами, код обработки и интерфейс. Модели скачиваются при первом запуске
# в ~/Library/Application Support/Meeting Agent.
#
#   ./build_app.sh            — собрать
#   ./build_app.sh --install  — собрать и заменить копию в /Applications
#
# Пакеты Python берутся из .venv проекта (если версия Python совпадает) — без интернета.
# Иначе ставятся через pip (нужен интернет, ~1,5 ГБ).
set -euo pipefail
cd "$(dirname "$0")"

PY_TAG="20261021"
PY_VER="3.12.15"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/cpython-${PY_VER}%2B${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz"
APP="build/Meeting Agent.app"
RES="${APP}/Contents/Resources"
CACHE="build/cache"

say() { printf "\n\033[1m▸ %s\033[0m\n" "$1"; }

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
if [[ ! -x "${ENV}/bin/python3" ]]; then
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
cp -cR "${ENV}" "${RES}/python"
cp -R meeting_agent templates ui config.toml "${RES}/app/"
find "${RES}/app" -name "__pycache__" -type d -prune -exec rm -rf {} +

say "Компилирую оболочку (Swift)"
swiftc -O -target arm64-apple-macos13.0 app/main.swift app/Backend.swift app/Recording.swift \
  app/Platforms.swift app/CalendarLookup.swift app/WebWindow.swift recorder/Recorder.swift -o "${APP}/Contents/MacOS/Meeting Agent" 2>&1 \
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
  <key>NSScreenCaptureUsageDescription</key>
  <string>Meeting Agent записывает звук собеседников из Zoom и замечает начало встречи. Изображение экрана не сохраняется.</string>
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

if [[ "${1:-}" == "--install" ]]; then
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
