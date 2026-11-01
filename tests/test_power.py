import json
import tempfile
import time
import unittest
from pathlib import Path

from tailshare import power

from tests.test_bus import StaticNet, make_bus

MAC_ON_BATTERY = {"Type": "InternalBattery", "Is Present": True, "Power Source State": "Battery Power",
                  "Current Capacity": 57, "Max Capacity": 100, "Is Charging": False, "Time to Empty": 143,
                  "Time to Full Charge": 0, "LPM Active": True}
READING = {"battery": 57, "charging": False, "on_ac": False, "minutes": 143, "low_power": True, "at": 1}


class Share(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(power.parse_share(None), {"own": "full", "others": "off"})
        self.assertEqual(power.parse_share(""), {"own": "full", "others": "off"})
        self.assertEqual(power.parse_share("own=level,others=full"), {"own": "level", "others": "full"})
        self.assertEqual(power.parse_share(" others=level "), {"own": "full", "others": "level"})
        self.assertEqual(power.parse_share("own=off,"), {"own": "off", "others": "off"})
        for wrong in ("off", "OWN=off", " own = off ", "own=all", "own=,others=level", "me=full",
                      "others=full,own", "own=full=off"):  # set wrong: nothing at all
            self.assertEqual(power.parse_share(wrong), {"own": "off", "others": "off"}, wrong)

    def test_block(self):
        self.assertIsNone(power.block(READING, "off"))
        self.assertIsNone(power.block(None, "full"))
        self.assertEqual(power.block(READING, "level"), {"battery": 57, "charging": False, "on_ac": False, "at": 1})
        self.assertEqual(power.block(READING, "full"), READING)

    def test_clean(self):
        now = int(time.time())
        ok = {"battery": 4, "charging": False, "on_ac": False, "at": now}
        self.assertEqual(power.clean(ok), ok)
        self.assertEqual(power.clean({**ok, "minutes": 9, "x": "y"}), {**ok, "minutes": 9})
        self.assertEqual(power.clean({**ok, "minutes": -1}), ok)
        for bad in ({**ok, "battery": 101}, {**ok, "battery": "4"}, {**ok, "battery": True},
                    {**ok, "charging": 1}, {**ok, "on_ac": None}, {**ok, "at": now + 600}, {**ok, "at": "1"},
                    {**ok, "at": float("nan")}, {**ok, "at": float("-inf")}, {**ok, "at": 0}, {**ok, "at": -5},
                    {k: v for k, v in ok.items() if k != "at"}, [], "4%", None):
            self.assertIsNone(power.clean(bad), bad)


class Mac(unittest.TestCase):
    def test_on_battery(self):
        r = power.read_mac(lambda: [MAC_ON_BATTERY])
        self.assertEqual({**r, "at": 1}, READING)

    def test_charging_and_held(self):
        charging = {**MAC_ON_BATTERY, "Power Source State": "AC Power", "Is Charging": True, "Time to Full Charge": 40}
        self.assertEqual(power.read_mac(lambda: [charging])["minutes"], 40)
        self.assertTrue(power.read_mac(lambda: [charging])["on_ac"])
        held = {**charging, "Is Charging": False}  # optimised charging holding at 80%
        r = power.read_mac(lambda: [held])
        self.assertEqual((r["charging"], r["on_ac"], r["minutes"]), (False, True, None))

    def test_estimating_and_scale(self):
        r = power.read_mac(lambda: [{**MAC_ON_BATTERY, "Time to Empty": -1, "Current Capacity": 2500,
                                     "Max Capacity": 5000, "LPM Active": None}])
        self.assertEqual((r["battery"], r["minutes"], r["low_power"]), (50, None, None))

    def test_no_battery(self):
        self.assertIsNone(power.read_mac(lambda: []))
        self.assertIsNone(power.read_mac(lambda: [{"Type": "UPS", "Current Capacity": 90, "Max Capacity": 100}]))
        self.assertIsNone(power.read_mac(lambda: [{**MAC_ON_BATTERY, "Is Present": False}]))
        self.assertIsNone(power.read_mac(lambda: [{**MAC_ON_BATTERY, "Max Capacity": 0}]))

    @unittest.skipUnless(power.sys.platform == "darwin", "macOS")
    def test_iokit_answers(self):
        sources = power.iokit_sources()  # this machine's: a list either way, a battery or not
        self.assertIsInstance(sources, list)
        for d in sources:
            self.assertEqual(set(d), set(power._KEYS))


class Linux(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def supply(self, name: str, **files):
        d = self.root / name
        d.mkdir()
        for k, v in files.items():
            (d / k).write_text(f"{v}\n")

    def test_discharging(self):
        self.supply("ACAD", type="Mains", online=0)
        self.supply("BAT1", type="Battery", present=1, capacity=31, status="Discharging",
                    energy_now=10_000_000, energy_full=40_000_000, power_now=5_000_000)
        r = power.read_linux(self.root)
        self.assertEqual({**r, "at": 1}, {"battery": 31, "charging": False, "on_ac": False, "minutes": 120,
                                          "low_power": None, "at": 1})

    def test_charging_from_charge_files(self):
        self.supply("ACAD", type="Mains", online=1)
        self.supply("BAT1", type="Battery", capacity=50, status="Charging",
                    charge_now=2_000_000, charge_full=4_000_000, current_now=-1_000_000)
        r = power.read_linux(self.root)
        self.assertEqual((r["charging"], r["on_ac"], r["minutes"]), (True, True, 120))

    def test_full_and_not_charging_are_on_ac(self):
        self.supply("BAT0", type="Battery", capacity=100, status="Full")
        self.assertEqual(power.read_linux(self.root)["on_ac"], True)

    def test_no_rate(self):
        self.supply("BAT0", type="Battery", capacity=80, status="Discharging", energy_now=1, power_now=0)
        self.assertIsNone(power.read_linux(self.root)["minutes"])

    def test_no_battery(self):
        self.supply("ACAD", type="Mains", online=1)
        self.assertIsNone(power.read_linux(self.root))
        self.supply("BAT0", type="Battery", present=0, capacity=0)
        self.assertIsNone(power.read_linux(self.root))
        self.assertIsNone(power.read_linux(self.root / "missing"))

    def test_unreadable_capacity(self):
        self.supply("BAT0", type="Battery", capacity="", status="Unknown")
        self.assertIsNone(power.read_linux(self.root))


class Cache(unittest.TestCase):
    def test_reads_once_per_ttl(self):
        calls = []
        b = power.Battery(lambda: calls.append(1) or dict(READING), ttl=60)
        for _ in range(5):
            b.get()
        self.assertEqual(len(calls), 1)
        b.ttl = 0
        time.sleep(0.01)
        b.get()
        self.assertEqual(len(calls), 2)

    def test_off_never_reads(self):
        calls = []
        b = power.Battery(lambda: calls.append(1) or dict(READING))
        self.assertIsNone(b.for_caller(own=False, share={"own": "full", "others": "off"}))
        self.assertEqual(calls, [])


class Hello(unittest.TestCase):
    def setUp(self):
        net = StaticNet("air", usermap={"100.64.0.2": "nick@example.com", "100.64.0.9": "friend@example.com"})
        self.b = make_bus("air", net)
        self.b.battery = power.Battery(lambda: dict(READING))

    def hello(self, ip, share):
        old, power.SHARE = power.SHARE, share
        try:
            return self.b.hello(ip)
        finally:
            power.SHARE = old

    def test_own_full_others_off_by_default(self):
        share = power.parse_share(None)
        self.assertEqual(self.hello("100.64.0.2", share)["power"], READING)
        self.assertNotIn("power", self.hello("100.64.0.9", share))
        self.assertNotIn("power", self.hello("100.64.0.7", share))  # nobody the tailnet knows
        self.assertNotIn("power", self.hello("", share))

    def test_tagged_machine_has_no_own(self):
        net = StaticNet("srv", user="tagged-devices",
                        usermap={"100.64.0.5": "tagged-devices", "100.64.0.2": "nick@example.com"})
        self.b = make_bus("srv", net)
        self.b.battery = power.Battery(lambda: dict(READING))
        for ip in ("100.64.0.5", "100.64.0.2"):
            self.assertNotIn("power", self.hello(ip, power.parse_share(None)), ip)

    def test_machines_drops_a_nan_at(self):
        self.b.store.x("INSERT INTO peers(host, ip, version, last_ok, agents, power) VALUES(?,?,?,?,?,?)",
                       "deck", "100.64.0.3", "0.4", time.time(), "[]",
                       '{"battery": 9, "charging": false, "on_ac": false, "at": NaN}')
        rows = {m["host"]: m for m in self.b.machines()}
        self.assertIsNone(rows["deck"]["power"])
        json.dumps(self.b.machines(), allow_nan=False)

    def test_levels(self):
        share = power.parse_share("own=level,others=level")
        for ip in ("100.64.0.2", "100.64.0.9"):
            self.assertEqual(set(self.hello(ip, share)["power"]), {"battery", "charging", "on_ac", "at"})
        self.assertNotIn("power", self.hello("100.64.0.2", power.parse_share("own=off")))

    def test_no_battery_no_block(self):
        self.b.battery = power.Battery(lambda: None)
        self.assertNotIn("power", self.hello("100.64.0.2", power.parse_share(None)))

    def test_machines_keeps_only_checked_fields_of_a_peer(self):
        now = int(time.time())
        self.b.store.x("INSERT INTO peers(host, ip, version, last_ok, agents, power) VALUES(?,?,?,?,?,?)",
                       "deck", "100.64.0.3", "0.4", now, "[]",
                       json.dumps({"battery": 9, "charging": False, "on_ac": False, "at": now, "evil": "x"}))
        self.b.store.x("INSERT INTO peers(host, ip, version, last_ok, agents) VALUES(?,?,?,?,?)",
                       "old", "100.64.0.4", "0.3", now, "[]")
        rows = {m["host"]: m for m in self.b.machines()}
        self.assertEqual(rows["deck"]["power"], {"battery": 9, "charging": False, "on_ac": False, "at": now})
        self.assertIsNone(rows["old"]["power"])
        self.assertEqual(rows["air"]["power"]["battery"], 57)


if __name__ == "__main__":
    unittest.main()
