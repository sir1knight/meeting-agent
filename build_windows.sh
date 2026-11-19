#!/bin/bash
# Собирает Windows-версию Meeting Agent на этом Mac: build/Meeting-Agent-Windows.zip.
# Внутри — папка «Meeting Agent»: Meeting Agent.exe (запускалка), python\ (Python 3.12 для Windows
# со всеми пакетами), app\ (код, интерфейс, шаблоны). Установка не нужна: распаковать и запустить.
#
#   ./build_windows.sh
#
# Нужны: uv (brew install uv) — подбирает и ставит пакеты под Windows; zig (brew install zig) —
# собирает Meeting Agent.exe. Пакеты: requirements-windows.txt → зафиксированы в requirements-windows.lock
# (пересоздать: uv pip compile … — команда ниже, в шаге «Пакеты»).
set -euo pipefail
cd "$(dirname "$0")"

PY_TAG="20261021"
PY_VER="3.12.15"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/cpython-${PY_VER}%2B${PY_TAG}-x86_64-pc-windows-msvc-install_only.tar.gz"
LLAMA_INDEX="https://abetlen.github.io/llama-cpp-python/whl/cpu"
# Пакеты с кодом на C/C++: только готовые сборки для Windows (из исходников на Mac получились бы библиотеки macOS).
NO_BUILD="--only-binary llama-cpp-python --only-binary torch --only-binary ctranslate2"
CACHE="build/wincache"
OUT="build/windows/Meeting Agent"
ZIP="build/Meeting-Agent-Windows.zip"

say() { printf "\n\033[1m▸ %s\033[0m\n" "$1"; }
for tool in uv zig; do
  command -v "$tool" >/dev/null || { echo "Нужен $tool: brew install $tool"; exit 1; }
done
mkdir -p "${CACHE}"

say "Python ${PY_VER} для Windows"
if [[ ! -s "${CACHE}/python-win.tar.gz" ]]; then
  curl -fL --retry 5 -C - -o "${CACHE}/python-win.tar.gz.part" "${PY_URL}"
  mv "${CACHE}/python-win.tar.gz.part" "${CACHE}/python-win.tar.gz"
fi

say "Пакеты Python для Windows"
if [[ ! -s requirements-windows.lock || requirements-windows.txt -nt requirements-windows.lock ]]; then
  uv pip compile requirements-windows.txt --python-platform x86_64-pc-windows-msvc --python-version 3.12 \
    --extra-index-url "${LLAMA_INDEX}" --index-strategy unsafe-best-match ${NO_BUILD} --no-header --no-annotate \
    -o requirements-windows.lock
fi
LOCK_HASH=$(shasum requirements-windows.lock | cut -c1-12)
SITE="${CACHE}/site-packages"
if [[ "$(cat "${CACHE}/site-packages.lock" 2>/dev/null)" != "${LOCK_HASH}" ]]; then
  # --no-deps: список уже полный и подобран под Windows (pip/uv на Mac иначе тянут пакеты для macOS).
  uv pip install --target "${SITE}" --python-platform x86_64-pc-windows-msvc --python-version 3.12 \
    --extra-index-url "${LLAMA_INDEX}" --index-strategy unsafe-best-match ${NO_BUILD} --no-deps -r requirements-windows.lock
  echo "${LOCK_HASH}" > "${CACHE}/site-packages.lock"
fi
# Проверка: внутри не должно оказаться библиотек для macOS (если пакет собрался из исходников на Mac).
if find "${SITE}" \( -name "*.so" -o -name "*.dylib" \) | grep -q .; then
  echo "В пакетах для Windows есть библиотеки macOS (.so/.dylib):"
  find "${SITE}" \( -name "*.so" -o -name "*.dylib" \) | head
  exit 1
fi

say "Собираю папку приложения"
rm -rf "build/windows"
mkdir -p "${OUT}"
tar xzf "${CACHE}/python-win.tar.gz" -C "${OUT}"          # → ${OUT}/python
cp -cR "${SITE}/" "${OUT}/python/Lib/site-packages/" 2>/dev/null || cp -R "${SITE}/" "${OUT}/python/Lib/site-packages/"
# Лишнее: заголовки и библиотеки для сборки C++, тесты, IDLE, Tk.
P="${OUT}/python"
rm -rf "${P}/Lib/site-packages/torch/include" "${P}/Lib/site-packages/torch/share" "${P}/include" \
       "${P}"/Lib/{test,idlelib,tkinter,turtledemo,ensurepip} "${P}/tcl" "${P}"/DLLs/{_tkinter.pyd,tcl*.dll,tk*.dll}
find "${P}/Lib/site-packages/torch/lib" -name "*.lib" -delete
find "${P}" \( -name "*.pdb" -o -path "*/llama_cpp/lib/*.lib" \) -delete   # отладочные символы и библиотеки для сборки
find "${P}" -name "__pycache__" -type d -prune -exec rm -rf {} +

mkdir -p "${OUT}/app"
cp -R meeting_agent templates ui config.toml "${OUT}/app/"
find "${OUT}/app" -name "__pycache__" -type d -prune -exec rm -rf {} +

say "Meeting Agent.exe"
(cd windows && zig cc -target x86_64-windows-gnu -O2 -municode -Wl,--subsystem,windows \
   launcher.c launcher.rc -o "../${OUT}/Meeting Agent.exe")
rm -f "${OUT}/Meeting Agent.pdb"
cp "windows/Прочитай меня.txt" "${OUT}/"

# Windows по умолчанию ограничивает путь 260 символами: предупреждаем, если запас мал.
LONGEST=$(cd "build/windows" && find . -type f | awk '{ print length($0) }' | sort -n | tail -1)
echo "Самый длинный путь внутри: ${LONGEST} символов (распаковывать в короткую папку, например C:\\MeetingAgent)"

say "Архив"
rm -f "${ZIP}"
(cd build/windows && ditto -c -k --norsrc --noextattr --keepParent "Meeting Agent" "../$(basename "${ZIP}")")
du -sh "${OUT}" | awk '{print "Папка: " $1}'
ls -lh "${ZIP}" | awk '{print "Архив: " $5 "  " $9}'
