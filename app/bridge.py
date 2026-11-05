#!/usr/bin/env python3
"""bmsups bridge: Eaton (NUT, raw) + JK BMS (jktool) -> virtual NUT UPS + dashboard.

* UPS thread  : polls the raw Eaton every UPS_POLL_S, runs the estimator, rewrites
                the dummy-ups .dev file, triggers killpower when due.
* BMS thread  : polls the BMS via jktool (BLE or serial) in a loop (single
                connection - the BMS allows only one), appends the CSV.
* HTTP server : dashboard + /api/state on HTTP_PORT.
Stdlib only.
"""
import csv
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from estimator import Config, Estimator, write_dev

E = os.environ.get
JKTOOL = E("JKTOOL", "/usr/local/bin/jktool")
BMS_TARGET = E("BMS_TARGET", "bt:hci0/dev_28_D4_1E_90_02_0F,ffe1")
UPS_RAW = E("UPS_RAW", "eaton-raw@localhost")
CONFIG_DIR = E("CONFIG_DIR", "/config")
CSV_PATH = E("CSV_PATH", os.path.join(CONFIG_DIR, "bms_log.csv"))
STATE_PATH = E("STATE_PATH", os.path.join(CONFIG_DIR, "state.json"))
DEV_PATH = E("VIRTUAL_DEV_PATH", "/run/nut/ups.dev")
NUT_ADMIN_USER = E("NUT_ADMIN_USER", "admin")
NUT_ADMIN_PASSWORD = E("NUT_ADMIN_PASSWORD", "")
PORT = int(E("HTTP_PORT", "8080"))
UPS_POLL_S = float(E("UPS_POLL_S", "2"))
BMS_PAUSE_S = float(E("BMS_PAUSE_S", "4"))
CSV_EVERY = float(E("CSV_EVERY_S", "10"))
HIST_EVERY = 5.0
KEEP = 24 * 3600
KEEP_LONG = 30 * 86400
MAX_CELLS = 16
CSV_HEAD = ["time", "pack_V", "current_A", "soc_pct", "remain_Ah", "cell_min_V", "cell_max_V",
            "diff_V", "bal_A", "mos_temp_C", "chg_mos", "dis_mos", "ups_status", "ups_batt_pct", "ups_load_W",
            "ups_runtime_s"] + [f"cell{n:02d}_V" for n in range(1, 17)] + ["virt_status", "virt_soc", "virt_runtime_s"]

logging.basicConfig(level=E("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bmsups")

lock = threading.Lock()
state = {"bms": None, "bms_t": 0, "bms_err": "starting", "ups": None, "ups_t": 0, "ups_err": "starting",
         "virtual": None}
history, hist_min = [], []
est = Estimator(Config.from_env(), STATE_PATH)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def read_bms():
    r = subprocess.run([JKTOOL, "-t", BMS_TARGET, "-f", "json", "read"], capture_output=True, text=True, timeout=40)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or not r.stdout.strip().startswith("{"):
        raise RuntimeError(out.splitlines()[-1][:160] if out else "no output")
    return json.loads(r.stdout)


def read_ups():
    r = subprocess.run(["upsc", UPS_RAW], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1][:160] if r.stderr.strip() else "upsc failed")
    d = {}
    for line in r.stdout.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            d[k] = v
    if "ups.status" not in d:
        raise RuntimeError("no ups.status")
    return d


def nut_admin(*args):
    if not NUT_ADMIN_PASSWORD:
        raise RuntimeError("NUT_ADMIN_PASSWORD not set")
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=15)
    log.warning("%s -> rc=%s %s", " ".join(a if a != NUT_ADMIN_PASSWORD else "***" for a in args),
                r.returncode, (r.stdout + r.stderr).strip())
    return r.returncode == 0


def killpower():
    cred = ["-u", NUT_ADMIN_USER, "-p", NUT_ADMIN_PASSWORD]
    delay = str(int(est.cfg.killpower_delay_s))
    nut_admin("upsrw", "-s", f"ups.delay.shutdown={delay}", *cred, UPS_RAW)
    if not nut_admin("upscmd", *cred, UPS_RAW, "shutdown.return"):
        nut_admin("upscmd", *cred, UPS_RAW, "load.off.delay", delay)


def sample(now):
    b, u, v = state["bms"], state["ups"], state["virtual"]
    s = {"t": now}
    if b and now - state["bms_t"] < 60:
        s.update(v=b["voltage"], i=b["current"], soc=b["soc"], rem=b["capacity_remaining"],
                 cmin=b["cell_min"], cmax=b["cell_max"], diff=b["cell_diff"], bal=b["balancing_current"],
                 mos=b["mosfet_temp"], chg=b["charging_mos"], dis=b["discharging_mos"],
                 pw=b["charging_power"] - b["discharging_power"], cv=b.get("cell_voltages") or [])
    if u:
        s.update(ustat=u.get("ups.status"), usoc=num(u.get("battery.charge")), uw=num(u.get("ups.realpower")),
                 uload=num(u.get("ups.load")))
    if v:
        s.update(vstat=v.get("ups.status"), vsoc=num(v.get("battery.charge")), vrt=num(v.get("battery.runtime")))
    return s


def migrate_csv():
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.reader(f))
    if not rows or rows[0] == CSV_HEAD:
        return
    pad = len(CSV_HEAD) - len(rows[0])
    tmp = CSV_PATH + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEAD)
        for r in rows[1:]:
            w.writerow(r + [""] * pad if pad > 0 else r)
    os.replace(tmp, CSV_PATH)
    log.info("migrated CSV header (%d new columns)", pad)


def load_csv():
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(CSV_HEAD)
        return
    now = time.time()
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            try:
                t = datetime.fromisoformat(row["time"]).timestamp()
            except Exception:
                continue
            if t < now - KEEP_LONG:
                continue
            v, i = num(row["pack_V"]), num(row["current_A"])
            p = {"t": t, "v": v, "i": i, "soc": num(row["soc_pct"]),
                 "pw": v * i if v is not None and i is not None else None,
                 "cmin": num(row["cell_min_V"]), "cmax": num(row["cell_max_V"]),
                 "usoc": num(row["ups_batt_pct"]), "uw": num(row["ups_load_W"]), "ustat": row["ups_status"],
                 "vsoc": num(row.get("virt_soc")), "vrt": num(row.get("virt_runtime_s")),
                 "vstat": row.get("virt_status")}
            hist_min.append(p)
            if t >= now - KEEP:
                history.append(p)


def append_csv(s):
    u, v = state["ups"] or {}, state["virtual"] or {}
    cv = s.get("cv") or []
    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow(
            [datetime.now().astimezone().isoformat(timespec="seconds"),
             s["v"], s["i"], s["soc"], s["rem"], s["cmin"], s["cmax"], s["diff"], s["bal"], s["mos"],
             "On" if s["chg"] else "Off", "On" if s["dis"] else "Off",
             u.get("ups.status", ""), u.get("battery.charge", ""), u.get("ups.realpower", ""),
             u.get("battery.runtime", "")]
            + [cv[n] if n < len(cv) else "" for n in range(MAX_CELLS)]
            + [v.get("ups.status", ""), v.get("battery.charge", ""), v.get("battery.runtime", "")])


def ups_loop():
    last_dev, last_dev_t, last_hist, last_save = None, 0.0, 0.0, time.time()
    while True:
        now = time.time()
        try:
            u = read_ups()
            with lock:
                state.update(ups=u, ups_t=now, ups_err=None)
        except Exception as e:
            with lock:
                if state["ups_err"] != str(e):
                    log.warning("UPS read failed: %s", e)
                state["ups_err"] = str(e)
        with lock:
            u = state["ups"] if now - state["ups_t"] < 30 else None
            if u is not None:
                v = est.step(u, now)
                prev = (state["virtual"] or {}).get("ups.status")
                state["virtual"] = v
                if prev != v["ups.status"]:
                    log.info("virtual status %s -> %s (raw %s, soc %s, %s)", prev, v["ups.status"],
                             u.get("ups.status"), v.get("battery.charge"), est.lb_reason or "ok")
                if v != last_dev or now - last_dev_t > 30:
                    try:
                        write_dev(DEV_PATH, v)
                        last_dev, last_dev_t = v, now
                    except OSError as e:
                        log.error("cannot write %s: %s", DEV_PATH, e)
                due = est.killpower_due(u, now)
                if due:
                    est.mark_killpower(now)
            else:
                due = False
            if now - last_hist >= HIST_EVERY and (state["bms"] or state["ups"]):
                last_hist = now
                history.append(sample(now))
                while history and history[0]["t"] < now - KEEP:
                    history.pop(0)
            if now - last_save > 600:
                last_save = now
                est.save()
        if due:
            log.warning("KILLPOWER: LB on battery (%s) -> shutdown.return, UPS output off in %.0f s",
                        est.lb_reason, est.cfg.killpower_delay_s)
            try:
                killpower()
            except Exception as e:
                log.error("killpower failed: %s", e)
        time.sleep(max(0.2, UPS_POLL_S - (time.time() - now)))


def bms_loop():
    last_csv = 0.0
    if BMS_TARGET.startswith("bt:"):
        time.sleep(float(E("BMS_START_DELAY_S", "8")))  # let bluetoothd register on D-Bus first
    while True:
        try:
            b = read_bms()
            now = time.time()
            with lock:
                if state["bms_err"]:
                    log.info("BMS read ok (soc %s%%, %.2f V)", b.get("soc"), b.get("voltage", 0))
                state.update(bms=b, bms_t=now, bms_err=None)
                est.feed_bms(b, now)
                if now - last_csv >= CSV_EVERY:
                    last_csv = now
                    s = sample(now)
                    hist_min.append(s)
                    while hist_min and hist_min[0]["t"] < now - KEEP_LONG:
                        hist_min.pop(0)
                    append_csv(s)
        except Exception as e:
            with lock:
                if state["bms_err"] != str(e):
                    log.warning("BMS read failed: %s", e)
                state["bms_err"] = str(e)
        time.sleep(BMS_PAUSE_S)


def api(mins):
    now = time.time()
    with lock:
        src = history if mins <= 1440 else hist_min
        pts = [p for p in src if p["t"] >= now - mins * 60]
        pts = pts[::max(1, len(pts) // 600)]
        return {"now": now,
                "bms": state["bms"], "bms_age": now - state["bms_t"] if state["bms_t"] else None,
                "bms_err": state["bms_err"],
                "ups": state["ups"], "ups_age": now - state["ups_t"] if state["ups_t"] else None,
                "ups_err": state["ups_err"],
                "virtual": state["virtual"], "est": est.summary(now),
                "config": {"lb_soc": est.cfg.lb_soc, "lb_runtime_s": est.cfg.lb_runtime_s,
                           "lb_cell_v": est.cfg.lb_cell_v, "bms_target": BMS_TARGET},
                "history": pts}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, q = self.path.partition("?")
        if path == "/api/state":
            mins = 60
            for kv in q.split("&"):
                if kv.startswith("mins="):
                    try:
                        mins = max(1, min(43200, int(kv[5:])))
                    except ValueError:
                        pass
            self.send(200, json.dumps(api(mins)).encode(), "application/json")
        elif path == "/healthz":
            with lock:
                ok = state["ups"] is not None and time.time() - state["ups_t"] < 30
            self.send(200 if ok else 503, b"ok" if ok else b"ups stale", "text/plain")
        elif path in ("/", "/index.html"):
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bmsdash.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        else:
            self.send(404, b"not found", "text/plain")


if __name__ == "__main__":
    os.makedirs(os.path.dirname(CSV_PATH) or ".", exist_ok=True)
    log.info("bmsups starting: ups=%s bms=%s dev=%s killpower=%s lb_soc=%s lb_runtime=%ss",
             UPS_RAW, BMS_TARGET, DEV_PATH, est.cfg.killpower, est.cfg.lb_soc, est.cfg.lb_runtime_s)
    if est.cfg.killpower and not NUT_ADMIN_PASSWORD:
        log.error("KILLPOWER enabled but NUT_ADMIN_PASSWORD is empty - killpower will fail")
    migrate_csv()
    load_csv()
    threading.Thread(target=ups_loop, daemon=True, name="ups").start()
    if BMS_TARGET.lower() != "none":
        threading.Thread(target=bms_loop, daemon=True, name="bms").start()
    # The dashboard must never take the virtual UPS down: if the port is taken,
    # keep NUT running and retry the HTTP bind periodically.
    while True:
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
        except OSError as e:
            log.error("dashboard: cannot bind :%d (%s) - NUT keeps running; set HTTP_PORT to a free port. "
                      "Retrying in 60 s", PORT, e)
            time.sleep(60)
            continue
        log.info("dashboard on :%d", PORT)
        srv.serve_forever()
