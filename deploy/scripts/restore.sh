#!/usr/bin/env bash
# Восстановление из бэкапа: restore.sh /путь/vpn-ДАТА.tar.gz.enc
# Пароль берётся из /opt/vpn/.env (BACKUP_PASSPHRASE) или спрашивается, если .env нет (новый сервер).
set -euo pipefail
FILE=${1:?Укажите файл бэкапа: restore.sh vpn-ДАТА.tar.gz.enc}
cd /opt/vpn
if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -z "${BACKUP_PASSPHRASE:-}" ]; then read -rsp "Пароль бэкапа: " BACKUP_PASSPHRASE; echo; export BACKUP_PASSPHRASE; fi
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE -in "$FILE" -out "$TMP/b.tgz"
tar -xzf "$TMP/b.tgz" -C "$TMP"
echo "В бэкапе: пользователей бота $(sqlite3 "$TMP/bot.db" 'select count(*) from users'), клиентов 3x-ui $(sqlite3 "$TMP/x-ui.db" 'select count(*) from client_traffics')"
if [ "${2:-}" != "-y" ]; then read -rp "Заменить текущие базы этими? Текущие сохранятся в /opt/vpn/backups/before-restore-*. [y/N] " a; [ "$a" = y ] || exit 1; fi
docker compose stop
mkdir -p backups xui/db bot-data config xui/cert
TS=$(date +%Y-%m-%d_%H%M%S)
[ -f xui/db/x-ui.db ] && cp xui/db/x-ui.db "backups/before-restore-$TS-x-ui.db"
[ -f bot-data/bot.db ] && cp bot-data/bot.db "backups/before-restore-$TS-bot.db"
rm -f xui/db/x-ui.db-wal xui/db/x-ui.db-shm bot-data/bot.db-wal bot-data/bot.db-shm
cp "$TMP/x-ui.db" xui/db/x-ui.db
cp "$TMP/bot.db" bot-data/bot.db; chown -R 1000:1000 bot-data
[ -f .env ] || { cp "$TMP/env" .env; chmod 600 .env; echo ".env восстановлен из бэкапа"; }
[ -f config/settings.yml ] || cp -r "$TMP/config/." config/
[ -f xui/cert/fullchain.pem ] || cp -r "$TMP/cert/." xui/cert/
docker compose up -d
echo "Готово. Проверьте: vpn status"
