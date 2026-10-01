#!/usr/bin/env bash
# Ночной бэкап: база 3x-ui + база бота + .env + настройки + сертификат.
# Архив шифруется паролем BACKUP_PASSPHRASE из .env, хранится 14 дней в /opt/vpn/backups
# и отправляется админам в Telegram.
set -euo pipefail
cd /opt/vpn
set -a; . ./.env; set +a
TS=$(date +%Y-%m-%d_%H%M)
OUT=/opt/vpn/backups; mkdir -p "$OUT"; chmod 700 "$OUT"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sqlite3 xui/db/x-ui.db ".backup '$TMP/x-ui.db'"
sqlite3 bot-data/bot.db ".backup '$TMP/bot.db'"
sqlite3 "$TMP/x-ui.db" "PRAGMA integrity_check;" | grep -qx ok
sqlite3 "$TMP/bot.db" "PRAGMA integrity_check;" | grep -qx ok
cp .env "$TMP/env"; cp -r config "$TMP/config"; cp -r xui/cert "$TMP/cert"
tar -czf "$TMP/b.tgz" -C "$TMP" x-ui.db bot.db env config cert
FILE="$OUT/vpn-$TS.tar.gz.enc"
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BACKUP_PASSPHRASE -in "$TMP/b.tgz" -out "$FILE"
chmod 600 "$FILE"
find "$OUT" -name 'vpn-*.tar.gz.enc' -mtime +14 -delete
SIZE=$(du -h "$FILE" | cut -f1)
USERS=$(sqlite3 bot-data/bot.db "select count(*) from users")
CLIENTS=$(sqlite3 xui/db/x-ui.db "select count(*) from client_traffics" 2>/dev/null || echo "?")
if [ "${1:-}" != "--no-send" ]; then
  for id in ${ADMIN_IDS//,/ }; do
    curl -s -m 60 -F chat_id="$id" -F document=@"$FILE" -F disable_notification=true \
      -F caption="🗄 Бэкап $TS ($SIZE): пользователей бота $USERS, клиентов 3x-ui $CLIENTS. Восстановление — README, раздел «Восстановление»." \
      "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" >/dev/null || echo "send to $id failed"
  done
fi
echo "$(date '+%F %T') ok $FILE $SIZE"
