#!/bin/bash
# Renders NUT config from env, prepares /config and /run, starts supervisord.
set -euo pipefail
log(){ echo "[entrypoint] $*"; }

mkdir -p "$CONFIG_DIR" /run/nut /run/dbus /etc/nut
chmod 0770 /run/nut

gen(){ tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24; }
SECRETS="$CONFIG_DIR/secrets.env"
if [ -z "${NUT_MONITOR_PASSWORD}" ] || [ -z "${NUT_ADMIN_PASSWORD}" ]; then
  [ -f "$SECRETS" ] || { echo "NUT_MONITOR_PASSWORD=$(gen)" > "$SECRETS"; echo "NUT_ADMIN_PASSWORD=$(gen)" >> "$SECRETS"; chmod 600 "$SECRETS"; }
  # shellcheck disable=SC1090
  . "$SECRETS"
  : "${NUT_MONITOR_PASSWORD:=$(grep ^NUT_MONITOR_PASSWORD "$SECRETS" | cut -d= -f2)}"
  : "${NUT_ADMIN_PASSWORD:=$(grep ^NUT_ADMIN_PASSWORD "$SECRETS" | cut -d= -f2)}"
  export NUT_MONITOR_PASSWORD NUT_ADMIN_PASSWORD
  log "using generated NUT passwords from $SECRETS (set NUT_MONITOR_PASSWORD / NUT_ADMIN_PASSWORD to override)"
fi

cat > /etc/nut/nut.conf <<C
MODE=netserver
C
cat > /etc/nut/ups.conf <<C
maxretry = 3
retrydelay = 5
pollinterval = 2

[${UPS_RAW_NAME}]
  driver = usbhid-ups
  port = auto
  vendorid = ${UPS_VENDORID}
  pollfreq = 30
  desc = "Eaton raw (lead-acid model - do not use for shutdown)"

[${UPS_NAME}]
  driver = dummy-ups
  port = /run/nut/${UPS_NAME}.dev
  mode = dummy-once
  desc = "Eaton 5SC + JK BMS (LFP) virtual UPS"
C
cat > /etc/nut/upsd.conf <<C
LISTEN 0.0.0.0 ${NUT_PORT}
MAXAGE 30
C
cat > /etc/nut/upsd.users <<C
[${NUT_MONITOR_USER}]
  password = ${NUT_MONITOR_PASSWORD}
  upsmon secondary

[${NUT_ADMIN_USER}]
  password = ${NUT_ADMIN_PASSWORD}
  actions = SET FSD
  instcmds = ALL
  upsmon primary
C
chmod 640 /etc/nut/*.conf /etc/nut/upsd.users

export UPS_RAW="${UPS_RAW_NAME}@localhost:${NUT_PORT}"
export VIRTUAL_DEV_PATH="/run/nut/${UPS_NAME}.dev"
rm -f "$VIRTUAL_DEV_PATH"

case "${BMS_TARGET}" in
  bt:*)
    export BT_ENABLED=true
    dbus-uuidgen --ensure=/etc/machine-id >/dev/null 2>&1 || true
    rm -f /run/dbus/pid
    if ls /sys/class/bluetooth/hci* >/dev/null 2>&1; then
      log "bluetooth adapters: $(ls /sys/class/bluetooth | tr '\n' ' ')"
    else
      log "WARNING: BMS_TARGET is Bluetooth but no adapter in /sys/class/bluetooth."
      log "         Check that the host kernel has btusb and the adapter is not passed to a VM,"
      log "         or switch to a serial link: BMS_TARGET=serial:/dev/ttyUSB0,115200"
    fi ;;
  *) export BT_ENABLED=false ;;
esac

[ -f "$CONFIG_DIR/bms_log.csv" ] && log "history: $(wc -l < "$CONFIG_DIR/bms_log.csv") CSV rows"
log "NUT: virtual '${UPS_NAME}', raw '${UPS_RAW_NAME}' on :${NUT_PORT}; dashboard :${HTTP_PORT}; BMS ${BMS_TARGET}"
exec /usr/bin/supervisord -n -c /etc/supervisor/bmsups.conf
