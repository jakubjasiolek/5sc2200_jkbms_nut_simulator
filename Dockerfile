# ---- build jktool (JK BMS CLI, crates.io jk_bms) -------------------------------
FROM rust:1-slim-trixie AS jktool
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libdbus-1-dev libudev-dev libsystemd-dev \
 && rm -rf /var/lib/apt/lists/*
ARG JK_BMS_VERSION=0.5.0
RUN cargo install jk_bms --version ${JK_BMS_VERSION} --locked --root /out

# ---- runtime -------------------------------------------------------------------
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      nut-server nut-client bluez dbus libdbus-1-3 libudev1 python3 supervisor usbutils procps ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/nut/*.conf /etc/nut/upsd.users
COPY --from=jktool /out/bin/jktool /usr/local/bin/jktool
COPY rootfs/ /
COPY app/ /app/
RUN chmod +x /usr/local/bin/*.sh

ENV CONFIG_DIR=/config \
    BMS_TARGET=bt:hci0/dev_28_D4_1E_90_02_0F,ffe1 \
    UPS_NAME=ups \
    UPS_RAW_NAME=eaton-raw \
    UPS_VENDORID=0463 \
    NUT_PORT=3493 \
    NUT_MONITOR_USER=monuser \
    NUT_MONITOR_PASSWORD= \
    NUT_ADMIN_USER=admin \
    NUT_ADMIN_PASSWORD= \
    HTTP_PORT=8080 \
    LB_SOC=20 \
    LB_RUNTIME_S=900 \
    LB_CELL_V=3.0 \
    KILLPOWER=1 \
    KILLPOWER_DELAY_S=180 \
    CAPACITY_AH=50 \
    V_NOM=51.2 \
    TZ=Europe/Warsaw

VOLUME /config
EXPOSE 3493 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('HTTP_PORT','8080'),timeout=4)"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
