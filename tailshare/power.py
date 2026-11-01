"""Battery level in /hello, so the Hive app (and `tailshare machines`) can show which machine is about to die.

Each machine decides what it shares with TAILSHARE_BATTERY="own=<mode>,others=<mode>", modes off, level
(battery, charging, on_ac, at) or full (also minutes and low_power). Own is a caller of this machine's
tailnet user; on a tagged machine (user tagged-devices) nobody is own. Default own=full,others=off. A machine without a battery, or one that cannot be read, sends
no block at all, the same as off.

The battery is read only when asked and kept for TTL seconds, so phones polling every 15 s add no work.
macOS: IOKit's IOPSCopyPowerSourcesInfo through ctypes (no pmset, no pyobjc). Linux: /sys/class/power_supply.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

TTL = 60.0
MODES = ("off", "level", "full")
LEVEL = ("battery", "charging", "on_ac", "at")
DEFAULT = {"own": "full", "others": "off"}
SYSFS = Path("/sys/class/power_supply")


def parse_share(value: str | None) -> dict:
    """{"own": mode, "others": mode} from TAILSHARE_BATTERY. Anything but exact side=mode parts shares nothing."""
    out = dict(DEFAULT)
    for part in (value or "").split(","):
        if not part.strip():
            continue
        side, eq, mode = part.strip().partition("=")
        if not eq or side not in out or mode not in MODES:
            return {"own": "off", "others": "off"}
        out[side] = mode
    return out


SHARE = parse_share(os.environ.get("TAILSHARE_BATTERY"))


def block(reading: dict | None, mode: str) -> dict | None:
    """What a caller with `mode` gets, or None for no block."""
    if not reading or mode not in ("level", "full"):
        return None
    return {k: reading[k] for k in LEVEL} if mode == "level" else dict(reading)


def clean(d) -> dict | None:
    """A peer's block, as `tailshare machines` may print it: the checked fields only, or None."""
    if not isinstance(d, dict):
        return None
    pct, at = d.get("battery"), d.get("at")
    if (type(pct) is not int or not 0 <= pct <= 100 or type(d.get("charging")) is not bool
            or type(d.get("on_ac")) is not bool or type(at) not in (int, float) or not math.isfinite(at)
            or not 0 < at <= time.time() + 120):
        return None
    out = {"battery": pct, "charging": d["charging"], "on_ac": d["on_ac"], "at": at}
    if type(d.get("minutes")) is int and d["minutes"] >= 0:
        out["minutes"] = d["minutes"]
    return out


# -- reading ------------------------------------------------------------------
def _reading(pct, charging, on_ac, minutes, low_power) -> dict | None:
    if type(pct) is not int:
        return None
    return {"battery": max(0, min(100, pct)), "charging": bool(charging), "on_ac": bool(on_ac),
            "minutes": minutes if type(minutes) is int and minutes >= 0 else None,
            "low_power": low_power if type(low_power) is bool else None, "at": int(time.time())}


def from_iokit(d: dict) -> dict | None:
    """A reading from one IOPS power source description (as a dict of its keys)."""
    if d.get("Type") != "InternalBattery" or d.get("Is Present") is False:
        return None
    cur, top = d.get("Current Capacity"), d.get("Max Capacity")
    if type(cur) is not int or type(top) is not int or top <= 0:
        return None
    on_ac, charging = d.get("Power Source State") == "AC Power", bool(d.get("Is Charging"))
    minutes = d.get("Time to Full Charge") if charging else None if on_ac else d.get("Time to Empty")
    return _reading(round(cur * 100 / top), charging, on_ac, minutes, d.get("LPM Active"))


_KEYS = ("Type", "Is Present", "Power Source State", "Current Capacity", "Max Capacity", "Is Charging",
         "Time to Empty", "Time to Full Charge", "LPM Active")


def iokit_sources() -> list[dict]:
    """Each power source's description, with only _KEYS, through IOKit."""
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    iok = ctypes.CDLL(ctypes.util.find_library("IOKit"))
    V = ctypes.c_void_p
    iok.IOPSCopyPowerSourcesInfo.restype = V
    iok.IOPSCopyPowerSourcesList.restype, iok.IOPSCopyPowerSourcesList.argtypes = V, [V]
    iok.IOPSGetPowerSourceDescription.restype, iok.IOPSGetPowerSourceDescription.argtypes = V, [V, V]
    cf.CFArrayGetCount.restype, cf.CFArrayGetCount.argtypes = ctypes.c_long, [V]
    cf.CFArrayGetValueAtIndex.restype, cf.CFArrayGetValueAtIndex.argtypes = V, [V, ctypes.c_long]
    cf.CFStringCreateWithCString.restype, cf.CFStringCreateWithCString.argtypes = V, [V, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFDictionaryGetValue.restype, cf.CFDictionaryGetValue.argtypes = V, [V, V]
    cf.CFGetTypeID.restype, cf.CFGetTypeID.argtypes = ctypes.c_ulong, [V]
    for f in ("CFNumberGetTypeID", "CFBooleanGetTypeID", "CFStringGetTypeID"):
        getattr(cf, f).restype = ctypes.c_ulong
    for f in ("CFNumberGetValue", "CFBooleanGetValue", "CFStringGetCString"):  # Boolean: one byte
        getattr(cf, f).restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [V, ctypes.c_int, V]
    cf.CFBooleanGetValue.argtypes = [V]
    cf.CFStringGetCString.argtypes = [V, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [V]
    utf8 = 0x08000100

    def value(v):
        if not v:
            return None
        t = cf.CFGetTypeID(v)
        if t == cf.CFNumberGetTypeID():
            n = ctypes.c_long()
            return n.value if cf.CFNumberGetValue(v, 4, ctypes.byref(n)) else None  # 4: kCFNumberSInt64Type
        if t == cf.CFBooleanGetTypeID():
            return bool(cf.CFBooleanGetValue(v))
        if t == cf.CFStringGetTypeID():
            b = ctypes.create_string_buffer(128)
            return b.value.decode() if cf.CFStringGetCString(v, b, 128, utf8) else None
        return None

    keys = {k: cf.CFStringCreateWithCString(None, k.encode(), utf8) for k in _KEYS}
    info = iok.IOPSCopyPowerSourcesInfo()
    lst = iok.IOPSCopyPowerSourcesList(info) if info else None
    try:
        out = []
        for i in range(cf.CFArrayGetCount(lst) if lst else 0):
            d = iok.IOPSGetPowerSourceDescription(info, cf.CFArrayGetValueAtIndex(lst, i))  # not owned
            if d:
                out.append({k: value(cf.CFDictionaryGetValue(d, s)) for k, s in keys.items()})
        return out
    finally:
        for x in (lst, info, *keys.values()):
            if x:
                cf.CFRelease(x)


def read_mac(sources: Callable[[], list[dict]] = iokit_sources) -> dict | None:
    for d in sources():
        r = from_iokit(d)
        if r:
            return r
    return None


def read_linux(root: Path = SYSFS) -> dict | None:
    def get(p: Path, name: str) -> str:
        try:
            return (p / name).read_text().strip()
        except OSError:
            return ""

    def num(p: Path, name: str) -> int | None:
        v = get(p, name)
        return int(v) if v.lstrip("-").isdigit() else None

    try:
        supplies = sorted(root.iterdir())
    except OSError:
        return None
    ac = any(get(p, "type") != "Battery" and get(p, "online") == "1" for p in supplies)
    for p in supplies:
        if get(p, "type") != "Battery" or get(p, "present") == "0":
            continue
        pct, status = num(p, "capacity"), get(p, "status")
        charging = status == "Charging"
        on_ac = ac or status in ("Charging", "Full", "Not charging")
        minutes = None
        for now, full, rate in (("energy_now", "energy_full", "power_now"), ("charge_now", "charge_full", "current_now")):
            n, f, r = num(p, now), num(p, full), num(p, rate)
            if n is not None and r:
                r = abs(r)
                if status == "Discharging":
                    minutes = n * 60 // r
                elif charging and f is not None:
                    minutes = max(0, f - n) * 60 // r
                break
        return _reading(pct, charging, on_ac, minutes, None)
    return None


def read() -> dict | None:
    try:
        if sys.platform == "darwin":
            return read_mac()
        if sys.platform.startswith("linux"):
            return read_linux()
    except Exception:  # noqa: BLE001 - no battery reading is no block
        return None
    return None


class Battery:
    """The last reading, at most TTL seconds old, read only when someone asks."""

    def __init__(self, reader: Callable[[], dict | None] = read, ttl: float = TTL) -> None:
        self.reader, self.ttl = reader, ttl
        self.lock = threading.Lock()
        self.last: tuple[float, dict | None] | None = None

    def get(self) -> dict | None:
        with self.lock:
            if self.last is None or time.monotonic() - self.last[0] > self.ttl:
                self.last = (time.monotonic(), self.reader())
            return self.last[1]

    def for_caller(self, own: bool, share: dict | None = None) -> dict | None:
        mode = (share or SHARE)["own" if own else "others"]
        return block(self.get(), mode) if mode != "off" else None
