#!/bin/bash
# Докачивает веса Qwen 2.5 7B (MLX, 4 бит) через curl и кладёт их в кэш Hugging Face.
# Нужен на нестабильной сети, где штатный загрузчик Hugging Face подвисает.
set -uo pipefail

REPO="mlx-community/Qwen2.5-7B-Instruct-4bit"
SHA="86110f368236b53cf4c2336f991a85703b17bcc60bb75f292b4002ec0219f071"
SIZE=4284346255
URL="https://huggingface.co/$REPO/resolve/main/model.safetensors"
D=~/.cache/huggingface/hub/models--${REPO/\//--}
REV=$(cat "$D/refs/main")
PART="$D/blobs/$SHA.curl"

mkdir -p "$D/blobs" "$D/snapshots/$REV"

# Берём за основу самый большой кусок, оставшийся от прошлой загрузки,
# только если его начало и конец совпадают с файлом на сервере.
if [[ ! -s "$PART" ]]; then
  best=$(ls -S "$D/blobs/$SHA".*.incomplete 2>/dev/null | head -1)
  if [[ -n "$best" ]]; then
    n=$(stat -f %z "$best")
    ok=1
    for off in 0 $((n - 1048576)); do
      a=$(dd if="$best" bs=1048576 skip=$((off / 1048576)) count=1 2>/dev/null | shasum | cut -c1-40)
      b=$(curl -sL -r "$off-$((off + 1048575))" "$URL" | shasum | cut -c1-40)
      [[ "$a" == "$b" ]] || ok=0
    done
    if [[ $ok == 1 ]]; then
      echo "Продолжаю с $((n / 1048576)) МБ, уже скачанных раньше"
      cp "$best" "$PART"
    fi
  fi
fi

for attempt in $(seq 1 50); do
  have=$(stat -f %z "$PART" 2>/dev/null || echo 0)
  [[ "$have" -ge "$SIZE" ]] && break
  echo "Попытка $attempt: скачано $((have / 1048576)) из $((SIZE / 1048576)) МБ"
  curl -L -C - --fail --silent --show-error --connect-timeout 20 \
       --speed-limit 50000 --speed-time 30 -o "$PART" "$URL"
  sleep 2
done

echo "Проверяю контрольную сумму…"
if [[ "$(shasum -a 256 "$PART" | cut -d' ' -f1)" != "$SHA" ]]; then
  echo "ОШИБКА: файл повреждён, удаляю его. Запустите скрипт ещё раз."
  rm -f "$PART"
  exit 1
fi
mv "$PART" "$D/blobs/$SHA"
rm -f "$D/blobs/$SHA".*.incomplete
ln -sf "../../blobs/$SHA" "$D/snapshots/$REV/model.safetensors"
echo "ГОТОВО: модель Qwen скачана"
