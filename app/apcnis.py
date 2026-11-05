"""Minimal apcupsd NIS (Network Information Server) so Unraid's built-in UPS
support (apcupsd, UPS type "Net") can follow the virtual UPS natively.

Protocol: TCP; every message is a 2-byte big-endian length + payload. The client
sends "status" (or "events"); the server answers with one framed message per
line and a zero-length frame at the end. apcupsd's net driver takes the UPS
state from STATFLAG (hex bit mask) and the values from BCHARGE / TIMELEFT /
LOADPCT / LINEV / BATTV etc.
"""
import socket
import socketserver
import struct
import threading
import time
from datetime import datetime

# apcupsd status bits (include/defines.h)
UPS_ONLINE = 0x08
UPS_ONBATT = 0x10
UPS_OVERLOAD = 0x20
UPS_BATTLOW = 0x40
UPS_COMMLOST = 0x100


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_status(v, raw, cfg, started, hostname, upsname):
    """v: virtual NUT var dict (or None when unknown); raw: raw Eaton dict."""
    v, raw = v or {}, raw or {}
    flags = v.get("ups.status", "").split()
    stat = 0
    words = []
    if not flags:
        stat |= UPS_COMMLOST
        words.append("COMMLOST")
    else:
        if "OL" in flags:
            stat |= UPS_ONLINE
            words.append("ONLINE")
        if "OB" in flags:
            stat |= UPS_ONBATT
            words.append("ONBATT")
        if "OVER" in flags:
            stat |= UPS_OVERLOAD
            words.append("OVERLOAD")
        if "LB" in flags:
            stat |= UPS_BATTLOW
            words.append("LOWBATT")
    now = datetime.now().astimezone()
    fmt = "%Y-%m-%d %H:%M:%S %z"
    rows = [
        ("APC", "001,036,0900"),
        ("DATE", now.strftime(fmt)),
        ("HOSTNAME", hostname),
        ("VERSION", "3.14.14 (bmsups NIS)"),
        ("UPSNAME", upsname),
        ("CABLE", "Ethernet Link"),
        ("DRIVER", "bmsups (Eaton USB + JK BMS)"),
        ("UPSMODE", "Stand Alone"),
        ("STARTTIME", datetime.fromtimestamp(started).astimezone().strftime(fmt)),
        ("MODEL", v.get("device.model") or raw.get("device.model") or "Eaton 5SC + JK BMS"),
        ("STATUS", " ".join(words) or "COMMLOST"),
    ]

    def add(key, val, unit, fmt_="{:.1f}"):
        x = _f(val)
        if x is not None:
            rows.append((key, (fmt_.format(x) + " " + unit).strip()))

    add("LINEV", raw.get("input.voltage"), "Volts")
    add("LOADPCT", raw.get("ups.load"), "Percent")
    add("BCHARGE", v.get("battery.charge"), "Percent")
    rt = _f(v.get("battery.runtime"))
    if rt is not None:
        rows.append(("TIMELEFT", f"{rt / 60:.1f} Minutes"))
    rows += [("MBATTCHG", f"{cfg.lb_soc:.0f} Percent"), ("MINTIMEL", f"{cfg.lb_runtime_s / 60:.0f} Minutes"),
             ("MAXTIME", "0 Seconds")]
    add("OUTPUTV", raw.get("output.voltage"), "Volts")
    add("ITEMP", v.get("battery.temperature"), "C")
    add("BATTV", v.get("battery.voltage"), "Volts")
    add("LINEFREQ", raw.get("input.frequency"), "Hz")
    rows.append(("NOMBATTV", f"{cfg.v_nom:.1f} Volts"))
    add("NOMPOWER", raw.get("ups.realpower.nominal"), "Watts", "{:.0f}")
    add("NOMAPNT", raw.get("ups.power.nominal"), "VA", "{:.0f}")
    rows += [("STATFLAG", f"0x{0x05000000 | stat:08X}"),
             ("SERIALNO", raw.get("ups.serial") or raw.get("device.serial") or "unknown"),
             ("FIRMWARE", raw.get("ups.firmware") or "unknown"),
             ("END APC", now.strftime(fmt))]
    return [f"{k:<9}: {val}\n" for k, val in rows]


def _frame(s):
    b = s.encode()
    return struct.pack(">H", len(b)) + b


class _Handler(socketserver.BaseRequestHandler):
    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def handle(self):
        self.request.settimeout(30)
        try:
            while True:
                hdr = self._recv_exact(2)
                if not hdr:
                    return
                (n,) = struct.unpack(">H", hdr)
                if n > 512:
                    return
                cmd = (self._recv_exact(n) or b"").decode(errors="replace").strip().lower()
                if cmd == "status":
                    lines = self.server.provider()
                elif cmd == "events":
                    lines = []
                else:
                    lines = ["Invalid command\n"]
                self.request.sendall(b"".join(_frame(x) for x in lines) + struct.pack(">H", 0))
        except (OSError, socket.timeout):
            return


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port, provider, log):
    """Blocking; retries the bind so a taken port never affects the UPS logic."""
    while True:
        try:
            srv = _Server(("0.0.0.0", port), _Handler)
        except OSError as e:
            log.error("apcupsd NIS: cannot bind :%d (%s); retrying in 60 s", port, e)
            time.sleep(60)
            continue
        srv.provider = provider
        log.info("apcupsd NIS on :%d (Unraid UPS Settings: cable Ether, type Net, device <ip>:%d)", port, port)
        srv.serve_forever()


def start(port, provider, log):
    t = threading.Thread(target=serve, args=(port, provider, log), daemon=True, name="apcnis")
    t.start()
    return t
