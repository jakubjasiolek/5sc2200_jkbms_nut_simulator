"""Replay tests for the estimator. Run: python3 -m unittest discover -s tests -v

Uses the real log (BMSUPS_LOG, default ~/bms_log.csv) when present for the
24.10 970 W discharge; synthetic cases always run.
"""
import csv
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from estimator import Config, Estimator  # noqa: E402

LOG = os.environ.get("BMSUPS_LOG", os.path.expanduser("~/bms_log.csv"))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def bms(soc, v=52.0, i=-1.5, cmin=3.25, dis=True):
    return {"soc": soc, "voltage": v, "current": i, "cell_min": cmin, "discharging_mos": dis}


def ups(status="OL", w=80):
    return {"ups.status": status, "ups.realpower": str(w), "battery.charge": "5", "battery.runtime": "18",
            "ups.model": "Eaton 5SC 2200i RT", "input.voltage": "0"}


class Synthetic(unittest.TestCase):
    def setUp(self):
        self.e = Estimator(Config(killpower=True, killpower_delay_s=180))

    def run_for(self, seconds, u, t0, step=2, b=None):
        t = t0
        out = None
        while t < t0 + seconds:
            if b is not None:
                self.e.feed_bms(b, t)
            out = self.e.step(u, t)
            t += step
        return out, t

    def test_strips_eaton_lb_and_reports_bms_soc(self):
        self.e.feed_bms(bms(94, i=-1.6), 0)
        out = self.e.step(ups("OB DISCHRG LB", 80), 0)
        self.assertEqual(out["ups.status"], "OB DISCHRG")
        self.assertEqual(out["battery.charge"], "94")
        self.assertEqual(out["battery.type"], "LiFePO4")
        # 80 W idle load: ~ (94-5)% * 2560 Wh / 83 W ~ 27 h
        self.assertGreater(int(out["battery.runtime"]), 20 * 3600)

    def test_lb_after_hold_and_killpower_once(self):
        out, t = self.run_for(20, ups("OB DISCHRG", 900), 0, b=bms(19, i=-20))
        self.assertNotIn("LB", out["ups.status"])  # hold time not reached
        out, t = self.run_for(20, ups("OB DISCHRG", 900), t, b=bms(19, i=-20))
        self.assertIn("LB", out["ups.status"])
        self.assertIn("SoC", self.e.lb_reason)
        self.assertFalse(self.e.killpower_due(ups("OL"), t))
        self.assertTrue(self.e.killpower_due(ups("OB DISCHRG"), t))
        self.e.mark_killpower(t)
        self.assertFalse(self.e.killpower_due(ups("OB DISCHRG"), t + 300))

    def test_lb_never_on_mains_and_clears(self):
        self.e.feed_bms(bms(5), 0)
        out, t = self.run_for(60, ups("OL CHRG"), 0)
        self.assertNotIn("LB", out["ups.status"])
        out, t = self.run_for(60, ups("OB DISCHRG", 900), t, b=bms(5, i=-20))
        self.assertIn("LB", out["ups.status"])
        out, t = self.run_for(4, ups("OL CHRG"), t, b=bms(5, i=5))
        self.assertEqual(out["ups.status"], "OL CHRG")
        self.assertFalse(self.e.killpower_due(ups("OL"), t + 1000))

    def test_stale_bms_extrapolates_then_lb(self):
        self.e.feed_bms(bms(80, i=-20), 0)
        out, t = self.run_for(300, ups("OB DISCHRG", 1000), 0)
        self.assertEqual(self.e.soc_src, "extrapolated")
        self.assertLess(self.e.soc, 80)
        self.assertNotIn("LB", out["ups.status"])
        out, t = self.run_for(400, ups("OB DISCHRG", 1000), t)
        self.assertIn("LB", out["ups.status"])
        self.assertIn("stale", self.e.lb_reason)

    def test_no_bms_ever_on_battery_is_lb(self):
        out, _ = self.run_for(40, ups("OB DISCHRG", 100), 0)
        self.assertIn("LB", out["ups.status"])

    def test_cell_voltage_and_mos_triggers(self):
        out, _ = self.run_for(40, ups("OB DISCHRG", 900), 0, b=bms(60, cmin=2.98, i=-20))
        self.assertIn("cell min", self.e.lb_reason)
        e2 = Estimator(Config())
        for t in range(0, 40, 2):
            e2.feed_bms(bms(60, dis=False, i=0), t)
            e2.step(ups("OB DISCHRG", 900), t)
        self.assertIn("MOS", e2.lb_reason)


@unittest.skipUnless(os.path.exists(LOG), "real log not available")
class ReplayOct24(unittest.TestCase):
    """970 W discharge on 2026-10-24: Eaton said LB/0% within minutes, BMS ran ~2 h."""

    def test_replay(self):
        e = Estimator(Config(killpower_delay_s=180))
        first_eaton_lb = first_virt_lb = None
        soc_at_lb = runtime_at_start = None
        with open(LOG, newline="") as f:
            for row in csv.DictReader(f):
                if not row["time"].startswith("2026-10-24T1"):
                    continue
                t = datetime.fromisoformat(row["time"]).timestamp()
                st = row["ups_status"]
                e.feed_bms({"soc": num(row["soc_pct"]), "voltage": num(row["pack_V"]),
                            "current": num(row["current_A"]), "cell_min": num(row["cell_min_V"]),
                            "discharging_mos": row["dis_mos"] == "On"}, t)
                out = e.step({"ups.status": st, "ups.realpower": row["ups_load_W"]}, t)
                if "OB" not in st:
                    continue
                if runtime_at_start is None:
                    runtime_at_start = num(out.get("battery.runtime"))
                if first_eaton_lb is None and "LB" in st.split():
                    first_eaton_lb = t
                if first_virt_lb is None and "LB" in out["ups.status"].split():
                    first_virt_lb, soc_at_lb = t, e.soc
        self.assertIsNotNone(first_eaton_lb)
        self.assertIsNotNone(first_virt_lb)
        gained = (first_virt_lb - first_eaton_lb) / 60
        print(f"\n  runtime at start {runtime_at_start/3600:.2f} h; Eaton LB -> virtual LB: +{gained:.0f} min;"
              f" SoC at virtual LB {soc_at_lb:.0f}% (dc/ac learned {e.dc_ac_ratio:.3f})")
        self.assertGreater(gained, 60)                   # we keep running > 1 h longer than Eaton
        self.assertTrue(15 <= soc_at_lb <= 30)           # LB around the 20 % / 15 min threshold
        self.assertTrue(1.5 * 3600 < runtime_at_start < 3 * 3600)  # ~2 h at ~1.1 kW DC


if __name__ == "__main__":
    unittest.main()
