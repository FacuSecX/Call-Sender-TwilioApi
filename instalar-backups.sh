#!/usr/bin/env bash
# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
set -Eeuo pipefail
umask 077
PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_USER="${SUDO_USER:-debian}"
BOT_SERVICE='bot-sms-twilio'
while (($#)); do
 case "$1" in
  --user) APP_USER="${2:?}";shift 2;;
  --service) BOT_SERVICE="${2:?}";shift 2;;
  *) printf 'Parámetro no admitido.\n' >&2;exit 1;;
 esac
done
[[ $EUID -eq 0 && "$PROJECT" =~ ^/[A-Za-z0-9_./-]+$ && "$APP_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ && "$APP_USER" != root && "$BOT_SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$ ]] || exit 1
source /etc/os-release
[[ "$ID" == debian && "$VERSION_ID" == 13 ]] || { printf 'Backups validados para Debian 13.\n' >&2;exit 1; }
GROUP="$(id -gn "$APP_USER")"
systemctl cat "$BOT_SERVICE.service" | grep -Fxq '# Managed by Bot SMS API Twilio installer' || { printf 'El servicio no pertenece al bot SMS.\n' >&2;exit 1; }
systemctl cat "$BOT_SERVICE.service" | grep -Fxq "WorkingDirectory=$PROJECT" || exit 1
INSTALL=/usr/local/lib/bot-sms-backups
UNIT=/etc/systemd/system/bot-sms-backups.service
if [[ -e "$INSTALL" ]]; then
 [[ ! -L "$INSTALL" && -f "$INSTALL/.managed-sms-backups" ]] || exit 1
fi
if [[ -e "$UNIT" ]]; then
 grep -Fxq '# Managed by Bot SMS backups installer' "$UNIT" || exit 1
fi
if [[ -f /etc/bot-sms-backups/config.json ]]; then
 python3 -c 'import json,sys;v=json.load(open(sys.argv[1]));assert v["project"]==sys.argv[2] and v["service"]==sys.argv[3]' /etc/bot-sms-backups/config.json "$PROJECT" "$BOT_SERVICE.service"
fi
LOG="/var/log/bot-sms-backups-installer-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
touch "$LOG";chmod 600 "$LOG"
if ! command -v veracrypt >/dev/null || ! command -v mega-get >/dev/null || ! command -v mkfs.exfat >/dev/null; then
 printf 'Instalando herramientas verificadas de cifrado y MEGA…\n'
 STAGE="$(mktemp -d /var/tmp/sms-backups-install.XXXXXXXX)"
 trap 'rm -rf -- "$STAGE"' EXIT
 ARCH="$(dpkg --print-architecture)"
 case "$ARCH" in
  amd64) VC_SHA=4afcaefaada73de258a9a34e03898be957221f0178290a84f0be57a8ede51c29;MEGA_SHA=d8c3c77d0417cba498fa6d4b3804fb657721afed072d6472c10e6b0b1b02dab9;;
  arm64) VC_SHA=f4c75c7a213e49361cb3d93446862bff97aef1b25f854386d61555273d99a045;MEGA_SHA=6d032193817581984a698f641a2f35703a677d5697923df9fda43f7a317d528b;;
  *) exit 1;;
 esac
 VC="veracrypt-console-1.26.30-Debian-13-$ARCH.deb"
 MEGA="megacmd_2.7.0-1.1_$ARCH.deb"
 {
  apt-get -q update
  apt-get -qy install --no-remove curl ca-certificates exfatprogs fuse3 dmsetup
  curl -fsSL --proto '=https' --tlsv1.2 --max-time 240 "https://github.com/veracrypt/VeraCrypt/releases/download/VeraCrypt_1.26.30/$VC" -o "$STAGE/$VC"
  printf '%s  %s\n' "$VC_SHA" "$STAGE/$VC" | sha256sum --check --status
  curl -fsSL --proto '=https' --tlsv1.2 --max-time 240 "https://mega.nz/linux/repo/Debian_13/$ARCH/$MEGA" -o "$STAGE/$MEGA"
  printf '%s  %s\n' "$MEGA_SHA" "$STAGE/$MEGA" | sha256sum --check --status
  chmod 644 "$STAGE/$VC" "$STAGE/$MEGA"
  apt-get -qy install --no-remove "$STAGE/$VC" "$STAGE/$MEGA"
 } >>"$LOG" 2>&1
fi
printf 'Preparando servicio aislado de backups…\n'
for path in /var/lib/bot-sms-backups /var/lib/bot-sms-backups/work /var/lib/bot-sms-backups/mega /var/spool/bot-sms-backups /run/bot-sms-backups /etc/bot-sms-backups; do
 [[ ! -L "$path" ]] || exit 1
done
install -d -m 755 "$INSTALL"
install -d -m 700 /var/lib/bot-sms-backups /var/lib/bot-sms-backups/work
install -d -m 700 /var/lib/bot-sms-backups/mega /etc/bot-sms-backups
install -d -m 770 -o root -g "$GROUP" /var/spool/bot-sms-backups
install -d -m 750 -o root -g "$GROUP" /run/bot-sms-backups
systemctl stop bot-sms-backups.service 2>/dev/null || true
for file in "$PROJECT"/backup_service/*.py; do
 [[ -f "$file" && ! -L "$file" ]] || exit 1
 install -m 644 -o root -g root "$file" "$INSTALL/${file##*/}"
done
touch "$INSTALL/.managed-sms-backups";chmod 600 "$INSTALL/.managed-sms-backups"
python3 - "$PROJECT" "$APP_USER" "$BOT_SERVICE.service" <<'PY'
import json,os,sys
from pathlib import Path
project,user,service=sys.argv[1:]
p=Path('/etc/bot-sms-backups/config.json')
p.write_text(json.dumps({'project':project,'user':user,'service':service}));p.chmod(0o600)
PY
cat >"$UNIT" <<EOF
# Managed by Bot SMS backups installer
[Unit]
Description=Backups cifrados del bot SMS
Wants=network-online.target
After=network-online.target
[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 -B $INSTALL/worker.py
UMask=0077
Environment=TZ=America/Argentina/Buenos_Aires
Restart=on-failure
RestartSec=5
TimeoutStopSec=300
PrivateTmp=true
PrivateMounts=true
NoNewPrivileges=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
RuntimeDirectory=bot-sms-backups
RuntimeDirectoryMode=0750
Group=$GROUP
[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
DROPIN="/etc/systemd/system/$BOT_SERVICE.service.d"
[[ ! -L "$DROPIN" ]] || exit 1
install -d -m 755 "$DROPIN"
if [[ -e "$DROPIN/backups-spool.conf" ]]; then
 grep -Fxq '# Managed by Bot SMS backups installer' "$DROPIN/backups-spool.conf" || exit 1
fi
printf '%s\n' '# Managed by Bot SMS backups installer' '[Service]' 'ReadWritePaths=/var/spool/bot-sms-backups' >"$DROPIN/backups-spool.conf"
chmod 644 "$DROPIN/backups-spool.conf"
systemd-analyze verify "$UNIT" >>"$LOG" 2>&1
systemctl daemon-reload
systemctl enable --now bot-sms-backups.service >>"$LOG" 2>&1
systemctl try-restart "$BOT_SERVICE.service" >>"$LOG" 2>&1
sleep 2
systemctl is-active --quiet bot-sms-backups.service
printf 'Backups instalados. Credenciales en .env; registros: %s\n' "$LOG"
