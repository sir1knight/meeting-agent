#!/bin/bash
# Сохраняет секрет в файл рядом с config.toml (права 600). Ввод не отображается на экране.
#   scripts/save_key.sh hf        — токен Hugging Face (разделение спикеров)
#   scripts/save_key.sh claude    — ключ Claude API (протоколы через облако)
cd "$(dirname "$0")/.."
case "${1:-}" in
  hf)     f=.hf_token;      prefix=hf_;     where="https://huggingface.co/settings/tokens" ;;
  claude) f=.anthropic_key; prefix=sk-ant-; where="https://platform.claude.com/settings/keys" ;;
  *) echo "Использование: $0 hf|claude"; exit 1 ;;
esac
echo "Скопируйте ключ здесь: $where"
read -r -s -p "Вставьте его ОДИН раз (Cmd+V) и нажмите Enter. Символы не видны: " k; echo
k="${k//[[:space:]]/}"
if [[ "$k" != "$prefix"* ]]; then echo "Это не похоже на ключ: он должен начинаться с $prefix"; exit 1; fi
if [[ $(grep -o "$prefix" <<<"$k" | wc -l) -gt 1 ]]; then echo "Ключ вставлен несколько раз подряд. Запустите ещё раз и вставьте один раз."; exit 1; fi
umask 077; printf "%s" "$k" > "$f"; unset k
echo "Сохранено в $(pwd)/$f"
