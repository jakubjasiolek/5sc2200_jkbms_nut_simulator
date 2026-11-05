import os
import socket
import struct
import sys
import threading
import time
import logging
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import apcnis  # noqa: E402
from estimator import Config  # noqa: E402


def parse(lines):
    return {l.split(":", 1)[0].strip(): l.split(":", 1)[1].strip() for l in lines}


class Status(unittest.TestCase):
    def st(self, status, **kw):
        v = {"ups.status": status, "battery.charge": "55", "battery.runtime": "3600", **kw}
        return parse(apcnis.build_status(v, {"input.voltage": "230"}, Config(), time.time(), "h", "ups"))

    def test_flags(self):
        self.assertEqual(self.st("OL")["STATUS"], "ONLINE")
        self.assertEqual(int(self.st("OL")["STATFLAG"], 16) & 0xFF, 0x08)
        self.assertEqual(self.st("OB DISCHRG")["STATUS"], "ONBATT")
        d = self.st("OB DISCHRG LB")
        self.assertEqual(d["STATUS"], "ONBATT LOWBATT")
        self.assertEqual(int(d["STATFLAG"], 16) & 0xFF, 0x50)
        self.assertEqual(d["BCHARGE"], "55.0 Percent")
        self.assertEqual(d["TIMELEFT"], "60.0 Minutes")

    def test_unknown_is_commlost(self):
        d = parse(apcnis.build_status(None, None, Config(), time.time(), "h", "ups"))
        self.assertEqual(d["STATUS"], "COMMLOST")
        self.assertNotIn("BCHARGE", d)


class Protocol(unittest.TestCase):
    def test_roundtrip(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        lines = ["STATUS   : ONLINE\n", "BCHARGE  : 99.0 Percent\n"]
        apcnis.start(port, lambda: lines, logging.getLogger("t"))
        for _ in range(50):
            try:
                c = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                time.sleep(0.05)
        with c:
            c.sendall(struct.pack(">H", 6) + b"status")
            got = []
            while True:
                (n,) = struct.unpack(">H", c.recv(2, socket.MSG_WAITALL))
                if n == 0:
                    break
                got.append(c.recv(n, socket.MSG_WAITALL).decode())
        self.assertEqual(got, lines)


if __name__ == "__main__":
    unittest.main()
