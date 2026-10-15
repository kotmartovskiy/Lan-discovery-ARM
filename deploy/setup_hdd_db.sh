#!/bin/bash
# setup_hdd_db.sh — перенос БД lan-discovery на HDD (минимизация записи
# на SD/eMMC: пишет только devices.db — её и уводим на диск).
#
# Использование (на устройстве, от root, при СМОНТИРОВАННОМ HDD в /srv):
#   bash deploy/setup_hdd_db.sh                          # v1.x (lan-discovery)
#   APP_DIR=/opt/lan-discovery-2.0 SERVICE=lan-discovery-2 \
#     bash deploy/setup_hdd_db.sh                        # v2.0
#
# Что делает:
#   1. бэкап drop-in юнита
#   2. останавливает $SERVICE
#   3. копирует devices.db в /srv/lan-discovery/ (с проверкой md5)
#   4. заменяет файл симлинком $APP_DIR/devices.db -> /srv/...
#   5. пишет drop-in hdd.conf: Requires=srv.mount (без диска панель не
#      стартует — БД никогда не создастся на корне)
#   6. запускает сервис и проверяет
set -e
TS=$(date +%Y%m%d-%H%M%S)
APP_DIR="${APP_DIR:-/opt/lan-discovery}"
SERVICE="${SERVICE:-lan-discovery}"
DROPIN_DIR="/etc/systemd/system/${SERVICE}.service.d"

if ! grep -q ' on /srv ' <(mount); then
  echo "ОШИБКА: HDD не смонтирован в /srv — переносить некуда. ABORT"
  exit 1
fi

mkdir -p "$DROPIN_DIR"
if [ -f "$DROPIN_DIR/hdd.conf" ]; then
  cp "$DROPIN_DIR/hdd.conf" "$DROPIN_DIR/hdd.conf.backup-$TS"
fi
systemctl stop "$SERVICE"

mkdir -p /srv/lan-discovery
if [ -f "$APP_DIR/devices.db" ] && [ ! -L "$APP_DIR/devices.db" ]; then
  cp -a "$APP_DIR/devices.db" /srv/lan-discovery/devices.db
  MD1=$(md5sum "$APP_DIR/devices.db" | cut -d' ' -f1)
  MD2=$(md5sum /srv/lan-discovery/devices.db | cut -d' ' -f1)
  if [ "$MD1" != "$MD2" ]; then
    echo "ОШИБКА: md5 не совпал — ABORT"
    exit 1
  fi
  rm -f "$APP_DIR/devices.db"
elif [ -L "$APP_DIR/devices.db" ]; then
  echo "симлинк уже есть — пропускаю перенос"
fi
ln -sfn /srv/lan-discovery/devices.db "$APP_DIR/devices.db"

cat > "$DROPIN_DIR/hdd.conf" <<'EOF'
[Unit]
# БД живёт ТОЛЬКО на HDD (/srv/lan-discovery/devices.db — симлинк из
# /opt/lan-discovery). Без смонтированного диска панель не стартует,
# чтобы БД не создалась/не писалась на корне (eMMC/SD).
Requires=srv.mount
After=srv.mount
EOF

systemctl daemon-reload
systemctl start "$SERVICE"
sleep 2
systemctl is-active "$SERVICE"
ls -la "$APP_DIR/devices.db"
echo "DONE: БД на HDD, зависимость Requires=srv.mount"
