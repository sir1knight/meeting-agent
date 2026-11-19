#!/bin/bash
# Создаёт постоянный сертификат подписи «Meeting Agent Local» в связке login.
# С ним macOS узнаёт приложение после любой пересборки и не сбрасывает разрешения
# на микрофон и запись экрана. Запускать один раз: scripts/make_signing_cert.sh
set -euo pipefail

NAME="Meeting Agent Local"
KC="$HOME/Library/Keychains/login.keychain-db"
OPENSSL=/usr/bin/openssl          # системный (LibreSSL): его .p12 macOS импортирует без доп. флагов
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

echo "1/5 Удаляю старые сертификаты «${NAME}» (если есть)"
for h in $(security find-certificate -a -c "${NAME}" -Z "${KC}" 2>/dev/null | awk '/SHA-1/{print $3}'); do
  security delete-certificate -Z "${h}" "${KC}" && echo "    удалён ${h}"
done

echo "2/5 Создаю ключ и сертификат подписи кода (на 10 лет)"
cat > "${TMP}/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = ${NAME}
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
subjectKeyIdentifier = hash
EOF
"${OPENSSL}" req -x509 -newkey rsa:2048 -nodes -days 3650 -config "${TMP}/cert.cnf" \
  -keyout "${TMP}/key.pem" -out "${TMP}/cert.pem" 2>/dev/null

echo "3/5 Упаковываю для связки ключей"
PASS="$("${OPENSSL}" rand -hex 16)"   # временный пароль .p12, после импорта не нужен
"${OPENSSL}" pkcs12 -export -inkey "${TMP}/key.pem" -in "${TMP}/cert.pem" -name "${NAME}" \
  -out "${TMP}/cert.p12" -passout "pass:${PASS}"

echo "4/5 Импортирую в связку login (ключ доступен утилите codesign)"
security import "${TMP}/cert.p12" -k "${KC}" -P "${PASS}" -T /usr/bin/codesign >/dev/null

echo "5/5 Отмечаю сертификат доверенным для подписи кода (macOS спросит пароль)"
security add-trusted-cert -r trustRoot -p codeSign -k "${KC}" "${TMP}/cert.pem"

echo
security find-identity -v -p codesigning | grep "${NAME}" \
  && echo "Готово: сертификат «${NAME}» создан." \
  || { echo "Сертификат не появился среди подписей — пришлите вывод этого скрипта."; exit 1; }
