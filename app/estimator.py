"""Virtual-UPS estimator: combines raw Eaton (NUT) data with JK BMS readings.

Pure logic, no I/O except optional state persistence, so it can be replayed
against the historical CSV in tests.
"""
import json
import os
import time
from dataclasses import dataclass, field


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _env(name, default, cast=float):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except ValueError:
        return default


@dataclass
class Config:
    capacity_ah: float = 50.0          # fallback if BMS does not report total capacity
    v_nom: float = 51.2                # 16S LFP nominal
    reserve_soc: float = 5.0           # % kept out of the runtime estimate
    lb_soc: float = 20.0               # LB when SoC <= this
    lb_runtime_s: float = 900.0        # LB when runtime <= this
    lb_cell_v: float = 3.00            # LB when weakest cell <= this under load
    lb_hold_s: float = 30.0            # condition must persist this long on battery
    bms_fresh_s: float = 60.0          # BMS data older than this -> extrapolate
    bms_stale_lb_s: float = 600.0      # BMS data older than this on battery -> LB
    dc_ac_ratio: float = 1.18          # initial DC-in / AC-out ratio (learned)
    idle_w: float = 15.0               # floor for DC draw estimate on battery
    killpower: bool = True
    killpower_delay_s: float = 180.0   # UPS-side countdown (ups.delay.shutdown)
    battery_type: str = "LiFePO4"

    @classmethod
    def from_env(cls):
        c = cls()
        for f_ in c.__dataclass_fields__.values():
            cur = getattr(c, f_.name)
            if isinstance(cur, bool):
                v = os.environ.get(f_.name.upper())
                if v is not None and v != "":
                    setattr(c, f_.name, v.strip().lower() in ("1", "true", "yes", "on"))
            elif isinstance(cur, float):
                setattr(c, f_.name, _env(f_.name.upper(), cur))
            elif isinstance(cur, str):
                setattr(c, f_.name, os.environ.get(f_.name.upper(), cur) or cur)
        return c


# Raw Eaton status flags we pass through. LB (lead-acid model) and RB are dropped.
PASS_FLAGS = ("OL", "OB", "CHRG", "DISCHRG", "BYPASS", "OFF", "OVER", "TRIM", "BOOST", "CAL", "FSD", "ALARM")

# Raw Eaton variables copied verbatim into the virtual UPS.
COPY_PREFIXES = ("input.", "output.", "outlet.", "ups.", "device.")
COPY_SKIP = {"ups.status", "ups.vendorid", "ups.productid"}


@dataclass
class Estimator:
    cfg: Config = field(default_factory=Config)
    state_path: str | None = None

    # learned / tracked state
    dc_ac_ratio: float = 0.0
    soc: float | None = None           # current best SoC estimate (%)
    soc_src: str = "none"              # bms | extrapolated | none
    runtime_s: float | None = None
    lb: bool = False
    lb_reason: str = ""
    _lb_since: float | None = None
    _lb_cond_since: float | None = None
    _last_t: float | None = None
    _bms_t: float | None = None
    _bms: dict | None = None
    killpower_sent: bool = False
    killpower_at: float | None = None

    def __post_init__(self):
        self.dc_ac_ratio = self.cfg.dc_ac_ratio
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    s = json.load(f)
                r = _num(s.get("dc_ac_ratio"))
                if r and 1.0 <= r <= 1.6:
                    self.dc_ac_ratio = r
            except Exception:
                pass

    def save(self):
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"dc_ac_ratio": round(self.dc_ac_ratio, 4)}, f)
        os.replace(tmp, self.state_path)

    # ---- inputs -------------------------------------------------------------
    def capacity(self):
        b = self._bms or {}
        c = _num(b.get("total_capacity"))
        return c if c and c > 1 else self.cfg.capacity_ah

    def feed_bms(self, b, t):
        """b: jktool JSON dict (or the subset used in CSV replay)."""
        self._bms, self._bms_t = b, t
        s = _num(b.get("soc"))
        if s is not None:
            self.soc, self.soc_src = s, "bms"

    def bms_age(self, now):
        return None if self._bms_t is None else now - self._bms_t

    # ---- main step ------------------------------------------------------------
    def step(self, ups, now):
        """ups: raw NUT var dict from the Eaton. Returns the virtual var dict."""
        cfg = self.cfg
        dt = 0.0 if self._last_t is None else max(0.0, min(now - self._last_t, 120.0))
        self._last_t = now

        raw = (ups or {}).get("ups.status", "")
        flags = raw.split()
        on_batt = "OB" in flags
        b = self._bms
        age = self.bms_age(now)
        fresh = b is not None and age is not None and age <= cfg.bms_fresh_s

        p_ac = _num((ups or {}).get("ups.realpower"))
        p_dc_bms = None
        if fresh:
            v, i = _num(b.get("voltage")), _num(b.get("current"))
            if v is not None and i is not None:
                p_dc_bms = -v * i  # positive = discharging

        # learn DC/AC ratio while discharging with fresh BMS data and real load
        if on_batt and p_dc_bms and p_ac and p_ac > 50 and p_dc_bms > 50:
            r = p_dc_bms / p_ac
            if 1.0 <= r <= 1.6:
                self.dc_ac_ratio += (r - self.dc_ac_ratio) * min(1.0, dt / 600.0)

        # discharge power estimate (W, positive)
        if on_batt and p_dc_bms is not None and p_dc_bms > 0:
            p_dc = p_dc_bms
        else:
            p_dc = max((p_ac or 0.0) * self.dc_ac_ratio, cfg.idle_w)

        # extrapolate SoC when BMS is stale and we are on battery
        cap = self.capacity()
        if not fresh and self.soc is not None and on_batt and dt > 0:
            self.soc = max(0.0, self.soc - (p_dc * dt / 3600.0) / (cap * cfg.v_nom) * 100.0)
            self.soc_src = "extrapolated"

        # runtime from remaining usable energy
        if self.soc is not None:
            usable_wh = max(0.0, (self.soc - cfg.reserve_soc) / 100.0 * cap * cfg.v_nom)
            rt = usable_wh / p_dc * 3600.0
            if self.runtime_s is None or not on_batt or dt == 0:
                self.runtime_s = rt
            else:
                a = min(1.0, dt / 60.0)
                self.runtime_s += (rt - self.runtime_s) * a
                self.runtime_s = min(self.runtime_s, rt * 1.5)  # never lag far above
        else:
            self.runtime_s = None

        # low-battery decision
        reasons = []
        if on_batt:
            if self.soc is None:
                reasons.append("no BMS data")
            else:
                if self.soc <= cfg.lb_soc:
                    reasons.append(f"SoC {self.soc:.0f}% <= {cfg.lb_soc:.0f}%")
                if self.runtime_s is not None and self.runtime_s <= cfg.lb_runtime_s:
                    reasons.append(f"runtime {self.runtime_s/60:.0f} min <= {cfg.lb_runtime_s/60:.0f} min")
            if fresh:
                cmin = _num(b.get("cell_min"))
                if cmin is not None and 0 < cmin <= cfg.lb_cell_v:
                    reasons.append(f"cell min {cmin:.3f} V <= {cfg.lb_cell_v:.2f} V")
                if b.get("discharging_mos") is False:
                    reasons.append("BMS discharge MOS off")
            if age is None or age > cfg.bms_stale_lb_s:
                reasons.append("BMS data stale" if age else "no BMS data")
        if reasons:
            if self._lb_cond_since is None:
                self._lb_cond_since = now
            if now - self._lb_cond_since >= cfg.lb_hold_s:
                if not self.lb:
                    self._lb_since = now
                self.lb, self.lb_reason = True, "; ".join(dict.fromkeys(reasons))
        else:
            self._lb_cond_since = None
            if not on_batt:  # LB clears only once mains is back
                self.lb, self.lb_reason, self._lb_since = False, "", None
                self.killpower_sent, self.killpower_at = False, None

        status = [f_ for f_ in flags if f_ in PASS_FLAGS]
        if self.lb:
            status.append("LB")
        if not status:
            status = ["OL"] if raw else ["OFF"]

        out = {k: v for k, v in (ups or {}).items() if k.startswith(COPY_PREFIXES) and k not in COPY_SKIP}
        out.update({
            "ups.status": " ".join(status),
            "ups.mfr": (ups or {}).get("ups.mfr", "EATON"),
            "device.type": "ups",
            "battery.type": cfg.battery_type,
            "battery.capacity": f"{cap:.0f}",
            "battery.charge.low": f"{cfg.lb_soc:.0f}",
            "battery.runtime.low": f"{cfg.lb_runtime_s:.0f}",
            "battery.charger.status": "discharging" if on_batt else (
                "charging" if "CHRG" in flags or (fresh and (_num(b.get("current")) or 0) > 0.2) else "resting"),
        })
        if self.soc is not None:
            out["battery.charge"] = f"{self.soc:.0f}"
        if self.runtime_s is not None:
            out["battery.runtime"] = f"{min(self.runtime_s, 7 * 86400):.0f}"
        if b:
            for k, src, fmt in (("battery.voltage", "voltage", "{:.2f}"), ("battery.current", "current", "{:.2f}"),
                                ("battery.temperature", "mosfet_temp", "{:.1f}"),
                                ("battery.voltage.nominal", None, None)):
                if src is None:
                    out[k] = f"{cfg.v_nom:.1f}"
                elif _num(b.get(src)) is not None:
                    out[k] = fmt.format(_num(b.get(src)))
        return out

    # ---- killpower ------------------------------------------------------------
    def killpower_due(self, ups, now):
        """True exactly once, as soon as LB is established on battery: the UPS is told to
        cut output after killpower_delay_s (counted by the UPS itself, so it still
        happens after Unraid has stopped this container) and to return on mains."""
        if not self.cfg.killpower or self.killpower_sent or not self.lb or self._lb_since is None:
            return False
        return "OB" in (ups or {}).get("ups.status", "").split()

    def mark_killpower(self, now):
        self.killpower_sent, self.killpower_at = True, now

    def summary(self, now):
        return {"soc": self.soc, "soc_src": self.soc_src, "runtime_s": self.runtime_s, "lb": self.lb,
                "lb_reason": self.lb_reason, "lb_since": self._lb_since, "dc_ac_ratio": round(self.dc_ac_ratio, 3),
                "bms_age": self.bms_age(now), "killpower_enabled": self.cfg.killpower,
                "killpower_delay_s": self.cfg.killpower_delay_s, "killpower_sent": self.killpower_sent,
                "killpower_at": self.killpower_at}


def write_dev(path, vars_):
    """Atomically write a dummy-ups .dev file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for k in sorted(vars_):
            v = str(vars_[k]).replace("\n", " ").strip()
            if v == "":
                continue
            f.write(f"{k}: {v}\n")
    os.replace(tmp, path)
