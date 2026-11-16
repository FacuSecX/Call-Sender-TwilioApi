#!/usr/bin/env bash
# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
set -Eeuo pipefail
umask 077

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SERVICE_NAME="bot-sms-twilio"
APP_USER="${SUDO_USER:-}"
NON_INTERACTIVE=0
ENV_SOURCE=""
START_SERVICE=1
CURRENT_STEP="Inicio"
LOG_FILE="/var/log/bot-sms-installer-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"

fail() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }
trap 'code=$?; printf "\nFalló: %s (código %s). Registro: %s\n" "$CURRENT_STEP" "$code" "$LOG_FILE" >&2; exit "$code"' ERR
while (($#)); do
    case "$1" in
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --service) SERVICE_NAME="${2:?Falta el nombre}"; shift 2 ;;
        --user) APP_USER="${2:?Falta el usuario}"; shift 2 ;;
        --env-file) ENV_SOURCE="${2:?Falta la ruta}"; shift 2 ;;
        --no-start) START_SERVICE=0; shift ;;
        --help) printf 'sudo bash instalar.sh [--service nombre] [--user usuario] [--non-interactive --env-file /ruta/privada.env] [--no-start]\n'; exit 0 ;;
        *) fail "Parámetro desconocido: $1" ;;
    esac
done
[[ $EUID -eq 0 ]] || fail "Ejecutá el instalador con sudo o como root."
[[ -r /etc/os-release ]] || fail "No se pudo detectar la distribución."
# shellcheck source=/dev/null
source /etc/os-release
[[ "$ID" == "debian" && "${VERSION_ID:-}" == "13" ]] || fail "Este instalador está validado para Debian 13. No se modifica esta distribución."
[[ "$PROJECT_DIR" =~ ^/[A-Za-z0-9_./-]+$ && "$PROJECT_DIR" != "/" ]] || fail "La carpeta Linux debe tener una ruta absoluta sin espacios ni caracteres especiales."
[[ -f "$PROJECT_DIR/requirements.txt" && -f "$PROJECT_DIR/smsbot/__main__.py" && -f "$PROJECT_DIR/configurar.py" ]] || fail "Subí la carpeta completa del proyecto antes de ejecutar instalar.sh."
command -v systemctl >/dev/null || fail "Se necesita systemd."
if [[ -z "$APP_USER" || "$APP_USER" == "root" ]]; then
    APP_USER="$(getent passwd 1000 | cut -d: -f1)"
fi
if (( !NON_INTERACTIVE )); then
    read -r -p "Nombre del servicio [$SERVICE_NAME]: " answer
    SERVICE_NAME="${answer:-$SERVICE_NAME}"
    read -r -p "Usuario Linux que ejecutará el bot [$APP_USER]: " answer
    APP_USER="${answer:-$APP_USER}"
fi
[[ "$SERVICE_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,60}$ ]] || fail "Nombre de servicio inválido. No incluyas .service."
[[ "$APP_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ && "$APP_USER" != "root" ]] || fail "Elegí una cuenta Linux existente, distinta de root."
id "$APP_USER" >/dev/null 2>&1 || fail "El usuario Linux no existe."
APP_GROUP="$(id -gn "$APP_USER")"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME.service"
if systemctl cat "$SERVICE_NAME.service" >/dev/null 2>&1; then
    [[ -f "$UNIT_PATH" ]] || fail "Ese nombre pertenece a otro servicio. Elegí otro."
    grep -Fxq '# Managed by Bot SMS API Twilio installer' "$UNIT_PATH" || fail "Ese nombre pertenece a otro servicio. Se conservó."
    grep -Fxq "WorkingDirectory=$PROJECT_DIR" "$UNIT_PATH" || fail "El servicio existente corresponde a otra carpeta. Se conservó."
fi
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
step() {
    CURRENT_STEP="$1"; shift
    printf '%s…\n' "$CURRENT_STEP"
    "$@" >>"$LOG_FILE" 2>&1
}
step '[1/6] Actualizar índice de paquetes' env DEBIAN_FRONTEND=noninteractive apt-get -q update
step '[2/6] Instalar Python y dependencias del sistema' env DEBIAN_FRONTEND=noninteractive apt-get -qy install python3 python3-venv python3-pip ca-certificates tzdata ffmpeg
if [[ -f "$UNIT_PATH" ]]; then
    step 'Detener únicamente esta instancia durante su actualización' systemctl stop "$SERVICE_NAME.service"
fi
step '[3/6] Preparar entorno Python aislado' python3 -m venv "$PROJECT_DIR/.venv"
step 'Instalar dependencias del bot' "$PROJECT_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"
chown -R "$APP_USER:$APP_GROUP" "$PROJECT_DIR"
chmod 700 "$PROJECT_DIR"
if [[ -n "$ENV_SOURCE" ]]; then
    [[ -f "$ENV_SOURCE" && ! -L "$ENV_SOURCE" ]] || fail "El archivo de configuración indicado no es un archivo regular."
    if [[ "$(readlink -f "$ENV_SOURCE")" != "$PROJECT_DIR/.env" ]]; then
        install -m 600 -o "$APP_USER" -g "$APP_GROUP" "$ENV_SOURCE" "$PROJECT_DIR/.env"
    fi
elif (( NON_INTERACTIVE )); then
    [[ -f "$PROJECT_DIR/.env" ]] || fail "Falta .env. Usá --env-file o ejecutá el modo interactivo."
else
    CURRENT_STEP='[4/6] Configurar Telegram y conexiones Twilio'
    printf '%s\n' "$CURRENT_STEP"
    runuser -u "$APP_USER" -- "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/configurar.py"
fi
chown "$APP_USER:$APP_GROUP" "$PROJECT_DIR/.env"
chmod 600 "$PROJECT_DIR/.env"
step '[4/6] Verificar configuración' runuser -u "$APP_USER" -- sh -c 'cd "$1" && exec .venv/bin/python -m smsbot --check-config' sh "$PROJECT_DIR"
step 'Verificar token con Telegram sin enviar SMS' runuser -u "$APP_USER" -- sh -c 'cd "$1" && exec .venv/bin/python -m smsbot --check-telegram' sh "$PROJECT_DIR"
CURRENT_STEP='[5/6] Crear servicio del bot'
printf '%s…\n' "$CURRENT_STEP"
cat >"$UNIT_PATH" <<EOF
# Managed by Bot SMS API Twilio installer
[Unit]
Description=Bot SMS API Twilio
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python -B -m smsbot --env $PROJECT_DIR/.env
Restart=on-failure
RestartSec=5
TimeoutStopSec=40
UMask=0077
Environment=PYTHONUNBUFFERED=1
Environment=TZ=America/Argentina/Buenos_Aires
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$PROJECT_DIR
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT_PATH"
step 'Validar unidad systemd' systemd-analyze verify "$UNIT_PATH"
step 'Cargar servicio' systemctl daemon-reload
step 'Habilitar arranque automático' systemctl enable "$SERVICE_NAME.service"
if (( START_SERVICE )); then
    step '[6/6] Iniciar bot' systemctl start "$SERVICE_NAME.service"
    sleep 2
    step 'Verificar que el servicio esté activo' systemctl is-active --quiet "$SERVICE_NAME.service"
else
    printf '[6/6] Servicio preparado sin iniciar.\n'
fi
if (( !NON_INTERACTIVE )); then
    read -r -p '¿Habilitar llamadas con MP3 de hasta 20 MB? s/n [n]: ' answer
    if [[ "${answer:-n}" == s ]]; then
        if runuser -u "$APP_USER" -- "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/configurar_audio.py"; then
            audio_info="$(runuser -u "$APP_USER" -- "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/configurar_audio.py" --binding)"
            read -r audio_host audio_port <<< "$audio_info"
            [[ "$audio_port" =~ ^[0-9]{4,5}$ ]] || fail 'El puerto de audios no es válido.'
            if [[ "$audio_host" == 0.0.0.0 ]] && command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
                step 'Permitir únicamente el puerto de los audios temporales' ufw allow "$audio_port/tcp" comment 'Bot SMS audio'
            fi
            if (( START_SERVICE )); then step 'Aplicar configuración MP3' systemctl restart "$SERVICE_NAME.service"; fi
        else
            printf 'MP3 pendiente; los SMS y las otras llamadas permanecen disponibles.\n' >&2
        fi
    fi
    read -r -p '¿Configurar backups cifrados en MEGA? s/n [n]: ' answer
    if [[ "${answer:-n}" == s ]]; then
        if runuser -u "$APP_USER" -- "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/configurar_backups.py" && bash "$PROJECT_DIR/instalar-backups.sh" --user "$APP_USER" --service "$SERVICE_NAME"; then
            printf 'Backups preparados.\n'
        else
            printf 'Backups pendientes; el resto del bot se conserva instalado.\n' >&2
        fi
    fi
fi
printf '\nInstalación terminada. Servicio: %s.service\nProyecto: %s\nRegistro: %s\nAbrí el bot en Telegram y enviá /start. No se envió ningún SMS.\n' "$SERVICE_NAME" "$PROJECT_DIR" "$LOG_FILE"
