#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y python3 python3-venv python3-pip nginx git

mkdir -p /opt
cd /opt
rm -rf qr-anketa
git clone https://github.com/Angy230592/qr-anketa.git
cd qr-anketa

python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(10))')"
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
PUBLIC_IP="$(curl -4 -s ifconfig.me || echo 212.193.15.84)"

cat >/opt/qr-anketa/.env <<EOF
DISABLE_TUNNEL=1
PORT=5050
ADMIN_USER=admin
ADMIN_PASSWORD=${ADMIN_PASSWORD}
SECRET_KEY=${SECRET_KEY}
PUBLIC_URL=http://${PUBLIC_IP}
EOF

mkdir -p /opt/qr-anketa/data
chown -R www-data:www-data /opt/qr-anketa
chown root:www-data /opt/qr-anketa/.env
chmod 640 /opt/qr-anketa/.env

cp /opt/qr-anketa/deploy/qr-anketa.service /etc/systemd/system/qr-anketa.service
cp /opt/qr-anketa/deploy/nginx.conf /etc/nginx/sites-available/qr-anketa
ln -sfn /etc/nginx/sites-available/qr-anketa /etc/nginx/sites-enabled/qr-anketa
rm -f /etc/nginx/sites-enabled/default

systemctl daemon-reload
systemctl enable --now qr-anketa
nginx -t
systemctl reload nginx

echo
echo "Готово. Админка: http://${PUBLIC_IP}/admin"
echo "Логин: admin"
echo "Пароль: ${ADMIN_PASSWORD}"
