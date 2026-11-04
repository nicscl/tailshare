"""Squads: the icon subset, validation, compare-and-set writes, what sharees see, and syncing between an owner's
machines. Everything runs on temp state and loopback buses; nothing touches ~/.tailshare or a real tmux."""

import hashlib
import http.client
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tailshare import bus, keys, reports_cli, squads, squads_cli, start, svg

from tests.test_bus import StaticNet, free_port, reg

TOKEN = "t" * 64
ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24px"><path d="M2 2L22 22z" fill="#fa0"/></svg>'


class Icons(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(svg.clean(ICON), '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                                          '<path d="M 2 2 L 22 22 z" fill="#fa0"/></svg>')
        self.assertEqual(svg.clean(""), "")
        ok = ('<svg viewBox="0 0 10 10"><g transform="translate(1,2) rotate(45)"><circle cx="5" cy="5" r="2" '
              'stroke-linecap="round" stroke-linejoin="bevel"/><polygon points="0,0 1,1 2,0"/></g></svg>')
        self.assertIn('stroke-linecap="round"', svg.clean(ok))
        self.assertIn('d="M 0 0 a 1 1 0 1 0 2 2"', svg.clean('<svg viewBox="0 0 4 4"><path d="M0 0a1 1 0 102 2"/></svg>'))

    def test_refused(self):
        bad = [
            '<!DOCTYPE svg [<!ENTITY x "y">]><svg viewBox="0 0 1 1">&x;</svg>',
            '<svg viewBox="0 0 1 1"><!-- hi --></svg>',
            '<?xml-stylesheet href="x"?><svg viewBox="0 0 1 1"/>',
            '<svg viewBox="0 0 1 1"><script>alert(1)</script></svg>',
            '<svg viewBox="0 0 1 1" onload="x()"/>',
            '<svg viewBox="0 0 1 1"><image href="http://x/y.png"/></svg>',
            '<svg viewBox="0 0 1 1"><path d="M0 0" fill="url(#a)"/></svg>',
            '<svg viewBox="0 0 1 1"><path d="L0 0"/></svg>',
            '<svg viewBox="0 0 1 1"><circle r="1e9"/></svg>',
            '<svg viewBox="0 0 1 1"><circle r="nan"/></svg>',
            '<svg viewBox="0 0 1 1" xmlns:xlink="http://www.w3.org/1999/xlink"><g xlink:href="#a"/></svg>',
            '<svg viewBox="0 0 1 1">text</svg>',
            '<svg/>',
            '<g/>',
            '<svg viewBox="0 0 1 1"><svg viewBox="0 0 1 1"/></svg>',
            '<svg viewBox="0 0 1 1">' + "<g>" * 11 + "</g>" * 11 + "</svg>",
            '<svg viewBox="0 0 1 1"><path d="M0 0' + " L1 1" * 2001 + '"/></svg>',
            "x" * (svg.MAX_BYTES + 1),
            '<svg viewBox="0 0 1e-310 1"/>',  # would be stored as width 0
            '<svg viewBox="0 0 1 1"><circle r="0.0000001"/></svg>',
            '<svg viewBox="0 0 1 1"><path d="M\u0662 2"/></svg>',  # an Arabic-Indic digit
            '<svg viewBox="0 0 1 1"><path d="M1\u00a02"/></svg>',  # NBSP is no XML whitespace
            '<svg viewBox="0 0 1 1"><path d="M0 0" fill="&#x72;ed"/></svg>',
            '<svg viewBox="0 0 1 1"><path d="M0 0" fill="&amp;"/></svg>',
        ]
        for b in bad:
            with self.assertRaises(svg.Bad, msg=b[:60]):
                svg.clean(b)

    def test_only_xml_characters(self):
        """NUL and the rest outside XML 1.0's Char, a BOM, or text that is not UTF-8 never reach the parser: with
        NULs a parser may guess UTF-16 or UCS-4 and read past the checks."""
        for c in ("\0", "\x01", "\x1f", "\ufeff", "\ufffe", "\uffff", "\ud800", "\udfff"):
            for t in (c + ICON, ICON.replace("<path", c + "<path"), ICON.replace('fill="#fa0"', f'fill="#fa0{c}"')):
                with self.assertRaises(svg.Bad, msg=repr(c)):
                    svg.clean(t)
        utf16 = ICON.encode("utf-16-le").decode("latin-1")  # UTF-16 read as text: NULs between the letters
        with self.assertRaises(svg.Bad):
            svg.clean(utf16)
        with self.assertRaises(svg.Bad):  # whatever the declaration says, the bytes are read as UTF-8
            svg.clean('<?xml version="1.0" encoding="UTF-16"?>' + ICON)
        self.assertTrue(svg.clean("\t\n" + ICON + "\r\n"))
        for d in ('<?xml version="1.0" encoding="UTF-8"?>', "<?xml version='1.1' standalone='yes'?>"):
            self.assertTrue(svg.clean(d + ICON))
        for d in ('<?xml version="1.0" encoding="UTF-7"?>', '<?xml version="1.0" encoding="utf-8"?>',
                  '<?xml version="2.0"?>'):
            with self.assertRaises(svg.Bad, msg=d):
                svg.clean(d + ICON)
        with self.assertRaises(svg.Bad):  # UTF-7's "<"
            svg.clean('<?xml version="1.0" encoding="UTF-7"?>+ADw-svg viewBox="0 0 1 1"/>')
        with self.assertRaises(squads.Bad):  # nor in squad text, where a lone surrogate would fail to encode
            squads.clean_member(member(instructions="a\ud800b"))


def member(**kw):
    return {"name": "AppDev", "role": "iOS app", "host": "air", "kind": "claude", "folder": "/u/projects/app",
            "instructions": "You are AppDev.", "start": {"options": {"model": "opus"}, "prompt": ""}, **kw}


class Machine:
    """A bus with squads on temp state, its local and peer APIs on loopback."""

    def __init__(self, test, host, user="nick@example.com", peers=None, usermap=None):
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.net = StaticNet(host, peers=peers or {}, user=user, usermap=usermap or {"127.0.0.1": user})
        self.bus = bus.Bus(bus.Store(self.tmp / "bus.db"), self.net, free_port())
        self.bus.keys = mock.Mock(token=TOKEN, **{"ssh_info.return_value": {"enroll": False, "host_keys": []}})
        self.squads = self.bus.squads = squads.start(self.bus)

    def serve(self, test, local=False):
        srv = self.bus.listen("127.0.0.1", local=local)
        test.addCleanup(srv.server_close)
        test.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{self.bus.port}/bus/v1"

    def put(self, sid, **body):
        return self.squads.route("PUT", f"/squads/{sid}", {"base_rev": None, **body}, True, "127.0.0.1", {}, TOKEN)

    def patch(self, path, **body):
        return self.squads.route("PATCH", f"/squads/{path}", body, True, "127.0.0.1", {}, TOKEN)


class Writes(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self, "air")

    def test_create_read_and_compare_and_set(self):
        sq = self.m.put("sq1", name="Tailshare", color="teal", icon=ICON, members=[{"id": "m1", **member()}])["squad"]
        self.assertEqual((sq["owner"], sq["name"], sq["members"][0]["name"]), ("nick@example.com", "Tailshare", "AppDev"))
        self.assertEqual(sq["members"][0]["instructions_from"], "")
        with self.assertRaises(squads.Conflict):  # PUT only creates
            self.m.put("sq1", name="Again")
        rev = sq["members"][0]["rev"]
        out = self.m.patch("sq1/members/m1", role="lead", base_rev=rev)["member"]
        self.assertEqual((out["role"], out["name"]), ("lead", "AppDev"))  # partial: the rest stays
        with self.assertRaises(squads.Conflict) as c:  # someone wrote since rev
            self.m.patch("sq1/members/m1", role="other", base_rev=rev)
        self.assertEqual(c.exception.code, 409)
        self.assertEqual(c.exception.extra["member"]["role"], "lead")
        with self.assertRaises(squads.Bad):
            self.m.patch("sq1/members/m1", role="x")  # no base_rev
        with self.assertRaises(squads.Conflict):
            self.m.patch("sq1/members/m1", base_rev=None, **member())  # null base_rev only creates
        self.m.patch("sq1/members/m2", base_rev=None, **member(name="Other"))
        self.assertEqual(len(self.m.squads.squad("sq1")["members"]), 2)
        h = self.m.squads.squad("sq1")
        self.m.patch("sq1", name="Renamed", base_rev=h["rev"])
        self.assertEqual(self.m.squads.squad("sq1")["name"], "Renamed")

    def test_delete_and_recreate(self):
        sq = self.m.put("sq1", name="A", members=[{"id": "m1", **member()}])["squad"]
        self.m.patch("sq1/members/m1", deleted=True, base_rev=sq["members"][0]["rev"])
        self.assertEqual(self.m.squads.squad("sq1")["members"], [])
        self.assertTrue(self.m.squads.squad("sq1", tombstones=True)["members"][0]["deleted"])
        h = self.m.squads.squad("sq1")
        self.m.patch("sq1", deleted=True, base_rev=h["rev"])
        self.assertIsNone(self.m.squads.squad("sq1"))
        self.assertIn("sq1", self.m.squads.hello("nick@example.com", True)["squads"])  # the delete spreads
        self.m.put("sq1", name="B")  # a deleted squad may be made again
        self.assertEqual(self.m.squads.squad("sq1")["name"], "B")

    def test_validation(self):
        bad = [{"name": ""}, {"name": "x" * 41}, {"name": "A", "color": "red"}, {"name": "A", "icon": "<svg/>"},
               {"name": "A", "shares": [{"user": "-x"}]}, {"name": "A", "shares": [{"user": "a@b", "can": ["attach"]}]},
               {"name": "A", "members": [member(kind="bash")]}, {"name": "A", "members": [member(folder="rel/path")]},
               {"name": "A", "members": [member(host="a b")]}, {"name": "A", "members": [member(instructions="x" * 4097)]},
               {"name": "A", "members": [member(start={"options": {"Bad-Key": 1}})]},
               {"name": "A", "members": [member(start={"options": {}, "prompt": "", "extra": 1})]},
               {"name": "A", "members": [member(instructions_from="-bad")]},
               {"name": "A", "members": [member(), member(role="dup")]}]  # same address
        for i, b in enumerate(bad):
            with self.assertRaises(squads.Bad, msg=str(b)[:80]):
                self.m.put(f"sq{i}", **b)
        self.assertEqual(self.m.squads.ids(deleted=True), [])  # a refused PUT leaves nothing behind
        self.assertEqual(squads.clean_member(member(name="App‮Dev\x07"))["name"], "AppDev")
        with self.assertRaises(bus.BusError):
            self.m.put("../x", name="A")

    def test_from_mark(self):
        sq = self.m.put("sq1", name="A", members=[{"id": "m1", **member(instructions_from="alex@example.com",
                                                                         start={"prompt": "p", "prompt_from": "alex@example.com"})}])["squad"]
        m = sq["members"][0]
        self.assertEqual((m["instructions_from"], m["start"]["prompt_from"]), ("alex@example.com", "alex@example.com"))
        m = self.m.patch("sq1/members/m1", role="r", base_rev=m["rev"])["member"]
        self.assertEqual(m["instructions_from"], "alex@example.com")  # an edit of something else keeps it
        m = self.m.patch("sq1/members/m1", instructions="mine", base_rev=m["rev"])["member"]
        self.assertEqual(m["instructions_from"], "")  # the owner's edit clears it

    def test_caps(self):
        with mock.patch.object(squads, "MAX_SQUADS", 2):
            self.m.put("a", name="A")
            self.m.put("b", name="B")
            with self.assertRaises(squads.Bad):
                self.m.put("c", name="C")

    def test_local_writes_need_the_token(self):
        with self.assertRaises(bus.BusError) as c:
            self.m.squads.route("PUT", "/squads/sq1", {"name": "A", "base_rev": None}, True, "127.0.0.1", {}, "wrong")
        self.assertEqual(c.exception.code, 403)

    def test_no_single_owner(self):
        self.m.net.user = "tagged-devices"
        with self.assertRaises(bus.BusError):
            self.m.put("sq1", name="A")
        self.assertEqual(self.m.squads.hello("tagged-devices", True), {})


class Views(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self, "air", usermap={"100.1.1.1": "nick@example.com", "100.2.2.2": "alex@example.com",
                                               "100.3.3.3": "eve@example.com"})
        self.m.put("sq1", name="A", shares=[{"user": "alex@example.com"}], members=[{"id": "m1", **member()}])
        self.m.put("sq2", name="Private")

    def get(self, ip, path="/squads"):
        return self.m.squads.route("GET", path, {}, False, ip, {"tombstones": "1"})

    def test_sharee_sees_a_filtered_copy(self):
        own = self.get("100.1.1.1")["squads"]
        self.assertEqual([s["id"] for s in own], ["sq1", "sq2"])
        alex = self.get("100.2.2.2")["squads"]
        self.assertEqual([s["id"] for s in alex], ["sq1"])
        self.assertNotIn("shares", alex[0])
        self.assertEqual(alex[0]["members"][0]["folder"], "app")  # a basename unless show_paths
        self.assertEqual(self.get("100.3.3.3")["squads"], [])
        with self.assertRaises(bus.BusError):
            self.get("100.3.3.3", "/squads/sq1")
        self.assertEqual(set(self.m.squads.hello("alex@example.com", False)["squads"]), {"sq1"})
        self.assertEqual(self.m.squads.hello("eve@example.com", False), {})

    def test_only_the_owner_writes(self):
        for ip in ("100.2.2.2", "100.3.3.3"):
            with self.assertRaises(bus.BusError) as c:
                self.m.squads.route("PATCH", "/squads/sq1", {"name": "X", "base_rev": None}, False, ip, {})
            self.assertEqual(c.exception.code, 403)

    def test_agent_hint(self):
        self.assertNotIn("agent", self.m.squads.squad("sq1")["members"][0])
        a = reg(self.m.bus, "s1", "app", cwd="/u/projects/app", squad={"id": "sq1", "member": "m1"})
        self.assertEqual(a["squad"], {"id": "sq1", "member": "m1"})
        self.assertEqual(self.m.squads.squad("sq1")["members"][0]["agent"], a["address"])
        self.assertEqual(reg(self.m.bus, "s2", "x", squad={"id": "../", "member": "m1"})["squad"], None)
        hello = self.m.bus.hello("100.1.1.1")
        self.assertIn("squad", next(x for x in hello["agents"] if x["address"] == a["address"]))
        self.assertTrue(all("squad" not in x for x in self.m.bus.hello("100.2.2.2")["agents"]))  # owner only

    def test_a_tag_is_kept_only_for_the_member_itself(self):
        """/register is any local process: a tag sticks only for the member's kind and folder on its host, and
        only for one live agent (a `claude -p` in a member's shell inherits TAILSHARE_SQUAD/MEMBER)."""
        self.m.put("sq3", name="B", members=[{"id": "m1", **member(host="pro")}])
        for i, (harness, cwd, sid) in enumerate([("codex", "/u/projects/app", "sq1"), ("claude", "/tmp/elsewhere", "sq1"),
                                                  ("claude", "/u/projects/app", "sq3"), ("claude", "/u/projects/app", "nope")]):
            a = reg(self.m.bus, f"x{i}", "app", harness=harness, cwd=cwd, squad={"id": sid, "member": "m1"})
            self.assertIsNone(a["squad"], (harness, cwd, sid))
        self.assertEqual(self.m.squads.squad("sq1")["members"][0].get("agent", ""), "")
        real = reg(self.m.bus, "s1", "app", cwd="/u/projects/app/", squad={"id": "sq1", "member": "m1"})
        self.assertEqual(real["squad"], {"id": "sq1", "member": "m1"})
        child = reg(self.m.bus, "s2", "app", cwd="/u/projects/app", squad={"id": "sq1", "member": "m1"})
        self.assertIsNone(child["squad"])
        self.assertEqual(reg(self.m.bus, "s1", "app", cwd="/u/projects/app", squad={"id": "sq1", "member": "m1"})["squad"],
                         {"id": "sq1", "member": "m1"})  # the member itself, registering again
        self.assertEqual(self.m.squads.squad("sq1")["members"][0]["agent"], real["address"])

    def test_two_registrations_at_once_get_one_tag(self):
        self.m.put("sq5", name="R", members=[{"id": "m1", **member(host="air", folder="/u/projects/app")}])
        tag = {"id": "sq5", "member": "m1"}
        real = self.m.squads.check_tag
        barrier = threading.Barrier(2, timeout=2)

        def slow(*a):  # both check before either writes, unless the check is under the lock
            out = real(*a)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return out

        self.m.squads.check_tag = slow
        threads = [threading.Thread(target=reg, args=(self.m.bus, f"a{i}", "app"),
                                                  kwargs={"squad": tag, "cwd": "/u/projects/app"}) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        held = [r["id"] for r in self.m.bus.store.q("SELECT id FROM agents WHERE squad!=''")]
        self.assertEqual(len(held), 1, held)

    def test_sharees_get_no_start_options(self):
        sq = self.m.squads.squad("sq1")
        m = sq["members"][0]
        self.m.patch("sq1/members/m1", base_rev=m["rev"], start={
            "options": {"add_dirs": ["/u/secret/clientX"], "resume": "0b6c7d1e-aaaa", "permission_mode": "bypassPermissions"},
            "prompt": "go", "prompt_from": "alex@example.com"})
        alex = self.get("100.2.2.2", "/squads/sq1")["squad"]["members"][0]
        self.assertEqual(alex["start"], {"prompt": "go", "prompt_from": "alex@example.com", "skip": True,
                                         "auto": False})
        self.assertNotIn("/u/secret", json.dumps(alex))
        self.assertIn("add_dirs", self.get("100.1.1.1", "/squads/sq1")["squad"]["members"][0]["start"]["options"])
        m = self.m.squads.squad("sq1")["members"][0]
        self.m.patch("sq1/members/m1", base_rev=m["rev"], start={"options": {"permission_mode": "auto"}, "prompt": "go"})
        alex = self.get("100.2.2.2", "/squads/sq1")["squad"]["members"][0]
        self.assertEqual((alex["start"]["skip"], alex["start"]["auto"]), (False, True))  # its own label, not skipping

    def test_no_unsigned_writes_on_the_peer_api(self):
        """A program on this machine (by its tailnet IP) or on another of the owner's tailshare machines would
        otherwise skip the local API's token, and a machine that runs no bus looks like a device: unsigned, none
        of them write (Signed has the signed writes)."""
        self.m.bus.keys = real_keys(self.m)
        self.m.net.ip = "100.9.9.9"
        self.m.net.usermap.update({"100.9.9.9": "nick@example.com", "100.4.4.4": "nick@example.com"})
        self.m.net.ipmap.update({"100.9.9.9": "air", "100.4.4.4": "pro", "100.1.1.1": "iphone"})
        self.m.bus.store.x("INSERT INTO peers(host, ip, last_ok) VALUES('pro', '100.4.4.4', 1)")
        rev = self.m.squads.squad("sq1")["rev"]
        for ip in ("100.9.9.9", "100.4.4.4"):
            with self.assertRaises(bus.BusError) as c:
                self.m.squads.route("PATCH", "/squads/sq1", {"name": "X", "base_rev": rev}, False, ip, {})
            self.assertEqual(c.exception.code, 403, ip)
            self.assertIn("local API", str(c.exception))
        with self.assertRaises(bus.BusError) as c:
            self.m.squads.route("PATCH", "/squads/sq1", {"name": "From the phone", "base_rev": rev}, False, "100.1.1.1", {})
        self.assertEqual((c.exception.code, str(c.exception), c.exception.extra["reason"]), (403, "signature", "unsigned"))
        self.assertNotEqual(self.m.squads.squad("sq1")["name"], "From the phone")

    def test_remote_agent_hint_only_from_the_owners_machine(self):
        self.m.put("sq4", name="C", members=[{"id": "m1", **member(host="pro")}])
        tag = {"id": "sq4", "member": "m1"}
        self.m.bus.store.x("INSERT INTO peers(host, ip, last_ok, agents) VALUES('pro', '100.4.4.4', 1, ?)",
                           json.dumps([{"address": "pro/claude/app", "online": True, "last_seen": 1, "squad": tag}]))
        self.m.net.peers["pro"] = {"ip": "100.4.4.4", "user": "eve@example.com"}  # someone else's "pro"
        self.assertEqual(self.m.squads.squad("sq4")["members"][0].get("agent", ""), "")
        self.m.net.peers["pro"]["user"] = "nick@example.com"
        self.assertEqual(self.m.squads.squad("sq4")["members"][0]["agent"], "pro/claude/app")


class Replication(unittest.TestCase):
    def test_lww_merge_converges(self):
        a, b = Machine(self, "air"), Machine(self, "pro")
        a.put("sq1", name="A", members=[{"id": "m1", **member()}])
        b.squads.merge(a.squads.squad("sq1", tombstones=True))
        self.assertEqual(a.squads.sum("sq1"), b.squads.sum("sq1"))
        # edits to different members on different machines both survive
        a.patch("sq1/members/m1", role="from-a", base_rev=a.squads.squad("sq1")["members"][0]["rev"])
        b.patch("sq1/members/m2", base_rev=None, **member(name="Two"))
        da, db = a.squads.squad("sq1", tombstones=True), b.squads.squad("sq1", tombstones=True)
        a.squads.merge(db)
        b.squads.merge(da)
        self.assertEqual(a.squads.squad("sq1"), b.squads.squad("sq1"))
        self.assertEqual(a.squads.sum("sq1"), b.squads.sum("sq1"))
        self.assertEqual({m["role"] for m in a.squads.squad("sq1")["members"]}, {"from-a", "iOS app"})

    def test_writer_heals_a_copy_an_older_version_trimmed(self):
        a, b = Machine(self, "air"), Machine(self, "pro")
        a.put("sq1", name="A", members=[{"id": "m1", **member()}])
        b.squads.merge(a.squads.squad("sq1", tombstones=True))
        # as if b had stored it with a version that dropped a field: same rev, less content
        row = b.squads.store.one("SELECT doc FROM squad_members WHERE squad='sq1' AND id='m1'")
        trimmed = {k: v for k, v in json.loads(row["doc"]).items() if k != "role"}
        b.squads.store.x("UPDATE squad_members SET doc=? WHERE squad='sq1' AND id='m1'", json.dumps(trimmed))
        self.assertNotEqual(a.squads.sum("sq1"), b.squads.sum("sq1"))  # so sync pulls even at equal revs
        d = a.squads.squad("sq1", tombstones=True)
        self.assertEqual(b.squads.merge(d), 0)  # no source: the rev says nothing is newer
        self.assertEqual(b.squads.merge(d, "mini"), 0)  # not the writer: it can't speak for air's rev
        self.assertEqual(b.squads.merge(d, "air"), 1)
        self.assertEqual(a.squads.sum("sq1"), b.squads.sum("sq1"))
        self.assertEqual(b.squads.squad("sq1")["members"][0]["role"], a.squads.squad("sq1")["members"][0]["role"])
        self.assertEqual(b.squads.merge(d, "air"), 0)  # nothing left to heal

    def test_merge_refuses_bad_registers(self):
        a = Machine(self, "air")
        a.put("sq1", name="A")
        d = a.squads.squad("sq1", tombstones=True)
        future = int(time.time() * 1000) + 10 * 60_000
        d["members"] = [{"id": "m1", **member(), "rev": [future, "pro"]},  # too far ahead
                        {"id": "m2", **member(kind="bash"), "rev": [future // 2, "pro"]},  # invalid
                        {"id": "m3", **member(name="Ok"), "rev": [1, "pro"]}]
        self.assertEqual(a.squads.merge(d), 1)
        self.assertEqual([m["id"] for m in a.squads.squad("sq1")["members"]], ["m3"])
        self.assertEqual(a.squads.merge({**d, "owner": "eve@example.com"}), 0)  # not this owner's

    def test_sync_over_the_bus(self):
        """pro asks air's /hello like discover does and pulls what differs, including a register merged in
        under the max rev, which max_rev alone would miss."""
        air = Machine(self, "air")
        air.serve(self)
        pro = Machine(self, "pro", peers={"air": {"ip": "127.0.0.1", "port": air.bus.port, "online": True,
                                                  "user": "nick@example.com"}})
        air.put("sq1", name="A", members=[{"id": "m1", **member()}])
        pro.bus.discover(["air"])
        self.assertEqual(pro.squads.squad("sq1")["members"][0]["name"], "AppDev")
        # a register older than air's max arrives at air from a third machine
        d = air.squads.squad("sq1", tombstones=True)
        d["members"] = [{"id": "m9", **member(name="Late"), "rev": [d["rev"][0] - 5, "deck"]}]
        air.squads.merge(d)
        self.assertEqual(air.squads.max_rev("sq1"), pro.squads.max_rev("sq1"))
        pro.bus.discover(["air"])
        self.assertEqual(len(pro.squads.squad("sq1")["members"]), 2)
        self.assertEqual(air.squads.sum("sq1"), pro.squads.sum("sq1"))

    def test_no_pull_from_another_users_machine(self):
        """A peer of another tailnet user advertising sums is never asked for a squad."""
        pro = Machine(self, "pro")
        hello = {"squads": {"sq1": [9, "evil"]}, "squad_sums": {"sq1": "0" * 16}}
        with mock.patch.object(bus, "request") as req, mock.patch.object(pro.squads, "merge") as merge:
            pro.squads.sync_from("air", {"ip": "127.0.0.1", "port": 1, "user": "eve@example.com"}, hello)
            pro.squads.sync_from("air", {"ip": "127.0.0.1", "port": 1}, hello)  # no user known
            req.assert_not_called()
            merge.assert_not_called()
            pro.squads.sync_from("air", {"ip": "127.0.0.1", "port": 1, "user": "nick@example.com"}, hello)
            req.assert_called_once()  # the same user's machine is asked

    def test_merge_keeps_only_this_owners_squads(self):
        """A same-user peer's reply that carries a squad of another owner, with newer revs, stores nothing."""
        pro = Machine(self, "pro")
        now = int(time.time() * 1000)
        d = {"owner": "eve@example.com", "id": "sqE", "name": "Eve's", "rev": [now, "air"], "shares": [],
             "members": [{"id": "m1", **member(), "rev": [now, "air"]}]}
        for owner in ("eve@example.com", "", None, "nick@example.com "):
            self.assertEqual(pro.squads.merge({**d, "owner": owner}), 0, owner)
        self.assertEqual(pro.bus.store.q("SELECT * FROM squads"), [])
        self.assertEqual(pro.bus.store.q("SELECT * FROM squad_members"), [])
        with mock.patch.object(bus, "request", return_value={"squad": d}):
            pro.squads.sync_from("air", {"ip": "127.0.0.1", "port": 1, "user": "nick@example.com"},
                                 {"squad_sums": {"sqE": "1" * 16}})
        self.assertEqual(pro.squads.ids(deleted=True), [])
        self.assertEqual(pro.squads.merge({**d, "owner": "nick@example.com"}), 2)  # the same doc as its owner's

    def test_no_sync_from_another_user(self):
        air = Machine(self, "air", user="eve@example.com")
        air.serve(self)
        air.put("sq1", name="Eve's")
        pro = Machine(self, "pro", peers={"air": {"ip": "127.0.0.1", "port": air.bus.port, "online": True,
                                                  "user": "eve@example.com"}})
        pro.bus.discover(["air"])
        self.assertEqual(pro.squads.ids(deleted=True), [])

    def test_http_errors_carry_the_conflict(self):
        air = Machine(self, "air")
        url = air.serve(self, local=True)
        h = {"X-Tailshare-Keys": TOKEN}
        sq = bus.request(f"{url}/squads/sq1", {"name": "A", "base_rev": None}, headers=h, method="PUT")["squad"]
        bus.request(f"{url}/squads/sq1", {"name": "B", "base_rev": sq["rev"]}, headers=h, method="PATCH")
        with self.assertRaises(bus.BusError) as c:
            bus.request(f"{url}/squads/sq1", {"name": "C", "base_rev": sq["rev"]}, headers=h, method="PATCH")
        self.assertEqual((c.exception.code, str(c.exception), c.exception.extra["squad"]["name"]), (409, "conflict", "B"))
        with self.assertRaises(bus.BusError) as c:
            bus.request(f"{url}/squads/sq2", {"name": "", "base_rev": None}, headers=h, method="PUT")
        self.assertEqual((c.exception.code, c.exception.extra["field"]), (400, "name"))
        self.assertEqual(bus.request(f"{url}/squads")["squads"][0]["name"], "B")


class Edit(unittest.TestCase):
    def test_changes_are_per_register_with_their_rev(self):
        m = Machine(self, "air")
        sq = m.put("sq1", name="A", members=[{"id": "m1", **member()}, {"id": "m2", **member(name="Two")}])["squad"]
        new = squads_cli._editable(sq)
        new["name"] = "Renamed"
        new["members"][0]["role"] = "lead"
        del new["members"][1]
        new["members"].append({k: v for k, v in member(name="Three").items()})
        todo = squads_cli.changes(sq, new)
        self.assertEqual([(x[1].rsplit("/", 1)[-1], sorted(x[2])) for x in todo], [
            ("sq1", ["base_rev", "name"]), ("m1", ["base_rev", "role"]),
            (todo[2][1].rsplit("/", 1)[-1], sorted([*member(), "order", "base_rev"])), ("m2", ["base_rev", "deleted"])])
        for method, path, body in todo:
            m.squads.route(method, path, body, True, "127.0.0.1", {}, TOKEN)
        self.assertEqual([x["name"] for x in m.squads.squad("sq1")["members"]], ["AppDev", "Three"])
        self.assertEqual(squads_cli.changes(m.squads.squad("sq1"), squads_cli._editable(m.squads.squad("sq1"))), [])

    def test_agents_cannot_edit(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(ValueError):
            squads_cli._human_check()


class Tree(unittest.TestCase):
    """reports_to: the owner writes a tree of at most 3 levels; a merged-in knot reads as top-level members."""

    def setUp(self):
        self.m = Machine(self, "air")
        names = ["Manager", "Lead", "Dev", "Tester"]
        self.m.put("sq", name="S", members=[{"id": f"m{i}", **member(name=n, folder=f"/u/{n}", order=i)} for i, n in enumerate(names)])

    def set(self, mid, lead):
        rev = next(x for x in self.m.squads.squad("sq")["members"] if x["id"] == mid)["rev"]
        return self.m.patch(f"sq/members/{mid}", reports_to=lead, base_rev=rev)["member"]

    def reason(self, mid, lead):
        with self.assertRaises(squads.Bad) as c:
            self.set(mid, lead)
        self.assertEqual(c.exception.extra["field"], "reports_to")
        return c.exception.extra["reason"]

    def test_three_levels(self):
        self.assertEqual(self.set("m1", "m0")["reports_to"], "m0")
        self.set("m2", "m1")
        self.assertIn("levels", self.reason("m3", "m2"))  # a 4th level
        self.set("m3", "m1")
        ms = self.m.squads.squad("sq")["members"]
        self.assertEqual(squads.leads(ms), {"m0": None, "m1": "m0", "m2": "m1", "m3": "m1"})
        self.assertEqual(squads.branch(ms, "m1"), {"m1", "m2", "m3"})
        self.assertEqual(squads.branch(ms, "m0"), {"m0", "m1", "m2", "m3"})
        self.assertIsNone(self.set("m1", None)["reports_to"])

    def test_moving_a_lead_counts_its_reports(self):
        self.set("m2", "m1")  # Lead has a report
        self.set("m0", "m3")  # Manager under Tester
        self.assertIn("levels", self.reason("m1", "m0"))  # Tester > Manager > Lead > Dev

    def test_refusals(self):
        self.assertIn("itself", self.reason("m1", "m1"))
        self.assertIn("not a member", self.reason("m1", "nobody"))
        self.reason("m1", "../x")
        self.set("m1", "m0")
        self.assertIn("loop", self.reason("m0", "m1"))
        rev = next(x for x in self.m.squads.squad("sq")["members"] if x["id"] == "m1")["rev"]
        self.m.patch("sq/members/m1", deleted=True, base_rev=rev)
        self.assertIn("not a member", self.reason("m2", "m1"))  # a removed member leads no one
        with self.assertRaises(squads.Bad):
            self.m.put("new", name="N", members=[{"id": "a", **member(reports_to="b")}])
        self.assertIsNone(self.m.squads.squad("new"))

    def test_create_with_a_tree(self):
        sq = self.m.put("new", name="N", members=[{"id": "a", **member(reports_to="b")},
                                                   {"id": "b", **member(name="B")}])["squad"]
        self.assertEqual({x["id"]: x["reports_to"] for x in sq["members"]}, {"a": "b", "b": None})

    def test_merged_knots_read_as_top_level(self):
        ms = [{"id": "a", "reports_to": "b"}, {"id": "b", "reports_to": "a"}, {"id": "c", "reports_to": "gone"},
              {"id": "d", "reports_to": "c"}, {"id": "e", "reports_to": "d"}, {"id": "f", "reports_to": "e"},
              {"id": "g", "reports_to": "a"}, {"id": "x", "deleted": True}]
        self.assertEqual(squads.leads(ms), {"a": None, "b": None, "c": None, "d": "c", "e": "d", "f": None, "g": None})
        # the owner can still edit another member of a squad with a knot in it
        sq = self.m.squads.squad("sq")
        self.m.squads.merge({**sq, "members": [{**x, "reports_to": "m1" if x["id"] == "m0" else "m0" if x["id"] == "m1"
                                                else None, "rev": [x["rev"][0] + 1, "pro"]} for x in sq["members"]]})
        self.assertEqual(squads.leads(self.m.squads.squad("sq")["members"])["m0"], None)
        self.set("m2", "m3")
        rev = next(x for x in self.m.squads.squad("sq")["members"] if x["id"] == "m0")["rev"]
        self.m.patch("sq/members/m0", role="boss", base_rev=rev)

    def test_editor_round_trip(self):
        sq = self.m.squads.squad("sq")
        new = squads_cli._editable(sq)
        new["members"][1]["reports_to"] = "m0"
        todo = squads_cli.changes(sq, new)
        self.assertEqual([(p.rsplit("/", 1)[-1], sorted(b)) for _, p, b in todo], [("m1", ["base_rev", "reports_to"])])


class Starting(unittest.TestCase):
    """A start item naming a squad member takes everything from this machine's copy of the squad."""

    def setUp(self):
        self.m = Machine(self, "air")
        (self.m.tmp / "projects" / "app").mkdir(parents=True)
        root = self.root = (self.m.tmp / "projects").resolve()
        p = mock.patch.object(bus, "STATE_DIR", self.m.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.m.put("sq1", name="Tail Share", members=[
            {"id": "m1", **member(folder=str(root / "app"), instructions="Be AppDev.", start={"options": {"model": "opus"},
                                                                                              "prompt": "go"})},
            {"id": "m2", **member(name="Remote", host="pro", folder=str(root / "app"))}])
        self.cfg = {"roots": [str(root)], "skip_permissions": False, "max_running": 4,
                    "kinds": {"claude": {"bin": "/bin/echo", "options": ["model"]}}}
        b = mock.patch.object(start, "binary", return_value="/bin/echo")
        b.start()
        self.addCleanup(b.stop)

    def test_plan_from_the_squad(self):
        plan = start.build({"squad": {"id": "sq1", "member": "m1"}, "device_name": "iPhone"}, self.cfg, "safe", "air")
        self.assertEqual((plan["name"], plan["instructions"], plan["prompt"], plan["options"]),
                         ("AppDev", "Be AppDev.\n\n" + reports_cli.HOW, "go", {"model": "opus"}))
        self.assertEqual((plan["env"]["TAILSHARE_SQUAD"], plan["env"]["TAILSHARE_MEMBER"]), ("sq1", "m1"))
        self.assertEqual(plan["tmux"], {"session": "squad-tail-share", "window": "appdev"})
        self.assertEqual(plan["squad"], {"id": "sq1", "name": "Tail Share", "member": "m1"})
        plan = start.build({"squad": {"id": "sq1", "member": "m1"}, "prompt": "other"}, self.cfg, "safe", "air")
        self.assertEqual(plan["prompt"], "other")

    def test_refusals(self):
        for item, err in [({"squad": {"id": "sq1", "member": "m2"}}, "bad_request"),  # runs on pro
                          ({"squad": {"id": "nope", "member": "m1"}}, "bad_request"),
                          ({"squad": {"id": "sq1", "member": "m1"}, "instructions": "mine"}, "bad_request"),
                          ({"squad": "sq1"}, "bad_request")]:
            with self.assertRaises(start.Refused) as c:
                start.build(item, self.cfg, "safe", "air")
            self.assertEqual(c.exception.out["error"], err, item)
        reg(self.m.bus, "s1", "app", cwd=str(self.root / "app"), squad={"id": "sq1", "member": "m1"})
        with self.assertRaises(start.Refused) as c:
            start.build({"squad": {"id": "sq1", "member": "m1"}}, self.cfg, "safe", "air")
        self.assertEqual(c.exception.out["error"], "running")

    def test_a_claude_member_gets_a_session_id(self):
        con = start._db(self.m.tmp / "start.db")
        self.addCleanup(con.close)
        out = start.dry_run(con, [{"squad": {"id": "sq1", "member": "m1"}, "device_name": "iPhone"}], self.cfg, "safe",
                            "key", "air")
        plan = out[0]["plan"]
        self.assertRegex(plan["session"], r"^[0-9a-f-]{36}$")
        self.assertIn(f"--session-id={plan['session']}", plan["argv"])

    def test_codex_and_resuming_members(self):
        rid = "0b0e0f2a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
        self.m.put("sq2", name="Other", members=[
            {"id": "cx", **member(name="Cx", kind="codex", folder=str(self.root / "app"), start={})},
            {"id": "rs", **member(name="Rs", folder=str(self.root / "app"), start={"options": {"resume": rid}})}])
        cfg = {**self.cfg, "kinds": {"claude": {"bin": "/bin/echo", "options": ["resume"]},
                                     "codex": {"bin": "/bin/echo", "options": []}}}
        con = start._db(self.m.tmp / "start.db")
        self.addCleanup(con.close)
        out = start.dry_run(con, [{"squad": {"id": "sq2", "member": "cx"}}, {"squad": {"id": "sq2", "member": "rs"}}],
                            cfg, "safe", "key", "air")
        cx, rs = (r["plan"] for r in out)
        self.assertEqual(cx["session"], "")  # Codex picks its own
        self.assertFalse(any(a.startswith("--session-id") for a in cx["argv"]))
        self.assertEqual(rs["session"], rid)  # resuming: its own id, not a new one
        self.assertIn(f"--resume={rid}", rs["argv"])
        self.assertFalse(any(a.startswith("--session-id") for a in rs["argv"]))

    def test_one_owner_only(self):
        self.m.bus.store.x("INSERT INTO squads VALUES('old@example.com', 'sqO', '{}', 1, 'air', 0, 1)")
        with self.assertRaises(start.Refused):  # the machine's login changed: whose squads are these?
            start.build({"squad": {"id": "sq1", "member": "m1"}}, self.cfg, "safe", "air")

    def test_changed_since_the_last_start(self):
        con = start._db(self.m.tmp / "start.db")
        self.addCleanup(con.close)
        item = {"squad": {"id": "sq1", "member": "m1"}}
        plan = start.build(item, self.cfg, "safe", "air")
        self.assertEqual(start._changed(con, plan), "")
        con.execute("INSERT INTO member_starts VALUES('sq1','m1','old',?)", (time.time(),))
        self.assertIn("instructions changed", start._changed(con, plan))
        self.assertEqual(start._canon({**plan, "changed": "x"}), start._canon(plan))


ENV = {"PATH": "/usr/bin:/bin"}


def real_keys(m):
    """Real keys on the machine's temp state (its own authorized_keys file), with the fixture's token."""
    k = keys.Keys(m.bus, m.tmp / "signers", m.tmp / "authorized_keys", m.tmp / "keys.token")
    k.token = TOKEN
    return k


class Signed(unittest.TestCase):
    """Squad writes on the peer API carry a signature by one of the owner's device keys installed here."""

    def setUp(self):
        self.m = Machine(self, "air")
        b = self.m.bus
        b.keys = real_keys(self.m)
        self.m.put("sq1", name="S", members=[{"id": "m1", **member()}])
        b.net.ip = "100.9.9.9"
        b.net.usermap.update({ip: "nick@example.com" for ip in ("100.9.9.9", "100.96.0.1", "100.1.1.1")})
        b.net.usermap["100.7.7.7"] = "eve@example.com"
        b.net.ipmap.update({"100.9.9.9": "air", "100.96.0.1": "ipad", "100.1.1.1": "iphone"})
        b.store.x("INSERT INTO peers(host, ip, last_ok) VALUES('ipad', '100.96.0.1', 1)")  # it runs tailshare (iSH)
        self.start, self.plan, self.other = (self.keygen(n) for n in ("start", "plan", "other"))
        self.install("k0123456789ab", self.start, self.plan)  # the iPad's start key and its plan key, granted

    def keygen(self, name, kind=("-t", "ecdsa", "-b", "256")):
        path = self.m.tmp / name
        subprocess.run(["ssh-keygen", "-q", *kind, "-N", "", "-C", "", "-f", str(path)],
                       check=True, env={**ENV, "HOME": str(self.m.tmp)}, stdin=subprocess.DEVNULL)
        return path

    def install(self, key_id, key, plan=None, user="nick@example.com", grant=True):
        pub = lambda p: " ".join(Path(f"{p}.pub").read_text().split()[:2])  # noqa: E731
        self.m.bus.store.x("INSERT INTO ssh_keys(id, pubkey, fingerprint, device, node, user, mode, approved_by,"
                           " installed, plan_pubkey, squads) VALUES(?,?,?,?,?,?,?,?,?,?,?)", key_id, pub(key),
                           keys.fingerprint(pub(key)), "iPad", "n1", user, "start" if plan else "pane", "test", 1,
                           pub(plan) if plan else "", int(grant and bool(plan)))

    def sign(self, key, raw, key_id="k0123456789ab", method="PATCH", path="/bus/v1/squads/sq1", host="air",
             ts=None, nonce=None, namespace=keys.SQUADS_NAMESPACE):
        ts, nonce = str(int(time.time() if ts is None else ts)), nonce or secrets.token_hex(16)
        msg = "\n".join(["tailshare-squad-write/1", host, method, path, ts, nonce, hashlib.sha256(raw).hexdigest()])
        cp = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", namespace], input=msg.encode(),
                            capture_output=True, check=True, env={**ENV, "HOME": str(self.m.tmp)})
        sig = "".join(l for l in cp.stdout.decode().splitlines() if not l.startswith("-----"))
        return {"X-Tailshare-Key": key_id, "X-Tailshare-Time": ts, "X-Tailshare-Nonce": nonce, "X-Tailshare-Signature": sig}

    def write(self, ip, raw, headers, path="/squads/sq1", method="PATCH"):
        return self.m.squads.route(method, path, json.loads(raw), False, ip, {}, "", (raw, headers))

    def body(self, name):
        return json.dumps({"name": name, "base_rev": self.m.squads.squad("sq1")["rev"]}).encode()

    def refused(self, ip, raw, headers, **kw):
        with self.assertRaises(bus.BusError) as c:
            self.write(ip, raw, headers, **kw)
        self.assertEqual((c.exception.code, str(c.exception)), (403, "signature"), c.exception.extra)
        return c.exception.extra["reason"]

    def test_a_signed_write_lands_from_the_ipad_and_from_a_phone(self):
        raw = self.body("From the iPad")
        self.assertEqual(self.write("100.96.0.1", raw, self.sign(self.plan, raw))["squad"]["name"], "From the iPad")
        raw = self.body("From the phone")
        self.assertEqual(self.write("100.1.1.1", raw, self.sign(self.plan, raw))["squad"]["name"], "From the phone")
        mid = "m_new"
        raw = json.dumps({"base_rev": None, **member(name="New")}).encode()
        out = self.write("100.1.1.1", raw, self.sign(self.plan, raw, path=f"/bus/v1/squads/sq1/members/{mid}"),
                         path=f"/squads/sq1/members/{mid}")
        self.assertEqual(out["member"]["name"], "New")

    def test_only_a_granted_plan_key_signs(self):
        """Not the start key (Face ID on every save), not a pane key (the app's is a file in iSH), not a plan key
        the person did not grant squad edits."""
        raw = self.body("X")
        self.assertEqual(self.refused("100.96.0.1", raw, self.sign(self.start, raw)), "bad_signature")
        pane = self.keygen("pane", ("-t", "ed25519"))
        self.install("k0000000000aa", pane)
        self.assertEqual(self.refused("100.96.0.1", raw, self.sign(pane, raw, key_id="k0000000000aa")), "no_grant")
        start2, plan2 = self.keygen("start2"), self.keygen("plan2")
        self.install("k0000000000bb", start2, plan2, grant=False)
        self.assertEqual(self.refused("100.96.0.1", raw, self.sign(plan2, raw, key_id="k0000000000bb")), "no_grant")
        self.assertEqual(self.m.squads.squad("sq1")["name"], "S")

    def test_the_signature_covers_host_method_path_body_and_key(self):
        raw = self.body("X")
        self.install("k00000000000e", self.keygen("start3"), self.other)  # another granted pair
        for why, headers in [
            ("another machine", self.sign(self.plan, raw, host="pro")),
            ("another method", self.sign(self.plan, raw, method="PUT")),
            ("another path", self.sign(self.plan, raw, path="/bus/v1/squads/sq2")),
            ("another body", self.sign(self.plan, self.body("Y"))),
            ("another installed key's id", self.sign(self.plan, raw, key_id="k00000000000e")),
            ("another namespace", self.sign(self.plan, raw, namespace="tailshare-keys")),
            ("another pair's plan key", self.sign(self.other, raw)),
            ("a key not installed here", self.sign(self.keygen("stray"), raw)),
        ]:
            self.assertEqual(self.refused("100.96.0.1", raw, headers), "bad_signature", why)
        self.assertEqual(self.refused("100.96.0.1", raw, self.sign(self.plan, raw), method="PUT"), "bad_signature")
        self.m.net.host = "air2"  # signed for the name this machine has now
        self.assertEqual(self.refused("100.96.0.1", raw, self.sign(self.plan, raw)), "bad_signature")
        self.assertEqual(self.write("100.96.0.1", raw, self.sign(self.plan, raw, host="air2"))["squad"]["name"], "X")
        raw = self.body("Z")
        self.m.net.host = "air"
        h = self.sign(self.plan, raw)
        self.assertEqual(self.refused("100.96.0.1", raw, {**h, "X-Tailshare-Time": str(int(h["X-Tailshare-Time"]) + 1)}),
                         "bad_signature")
        self.assertEqual(self.refused("100.96.0.1", raw, {**h, "X-Tailshare-Signature": "!" * 40}), "bad_signature")
        self.assertEqual(self.m.squads.squad("sq1")["name"], "X")

    def test_a_put_signed_as_put_lands(self):
        raw = json.dumps({"base_rev": None, "name": "New"}).encode()
        out = self.write("100.1.1.1", raw, self.sign(self.plan, raw, method="PUT", path="/bus/v1/squads/sq9"),
                         path="/squads/sq9", method="PUT")
        self.assertEqual(out["squad"]["name"], "New")

    def test_signature_checks_are_limited_per_address(self):
        raw = self.body("X")
        bad = {**self.sign(self.plan, raw), "X-Tailshare-Signature": "A" * 40}
        for _ in range(keys.VERIFY_PER_MINUTE):
            self.assertEqual(self.refused("100.1.1.1", raw, bad), "bad_signature")
        with self.assertRaises(bus.BusError) as c:
            self.write("100.1.1.1", raw, self.sign(self.plan, raw))
        self.assertEqual((c.exception.code, c.exception.extra["reason"]), (429, "rate_limited"))
        self.assertEqual(self.write("100.96.0.1", raw, self.sign(self.plan, raw))["squad"]["name"], "X")  # another address
        self.m.bus.keys._verified["100.1.1.1"] = [time.time() - 61] * keys.VERIFY_PER_MINUTE  # a minute later
        raw = self.body("Y")
        self.assertEqual(self.write("100.1.1.1", raw, self.sign(self.plan, raw))["squad"]["name"], "Y")

    def test_old_replayed_and_unknown_keys(self):
        raw = self.body("X")
        self.assertEqual(self.refused("100.1.1.1", raw, self.sign(self.plan, raw, ts=time.time() - 120)), "expired")
        with self.assertRaises(bus.BusError) as c:
            self.write("100.1.1.1", raw, self.sign(self.plan, raw, ts=0))
        self.assertLess(abs(c.exception.extra["now"] - time.time()), 5)  # the device corrects its clock by it
        self.assertEqual(self.refused("100.1.1.1", raw, self.sign(self.plan, raw, ts=time.time() + 120)), "expired")
        h = self.sign(self.plan, raw)
        self.write("100.1.1.1", raw, h)
        self.assertEqual(self.refused("100.1.1.1", raw, h), "replayed")  # before the stale base_rev's 409
        self.assertEqual(self.refused("100.1.1.1", raw, self.sign(self.plan, raw, key_id="k999999999999")), "unknown_key")
        self.install("k00000000000e", self.keygen("start3"), self.other, user="eve@example.com")  # not the owner's
        raw = self.body("Y")
        self.assertEqual(self.refused("100.1.1.1", raw, self.sign(self.other, raw, key_id="k00000000000e")), "unknown_key")
        self.m.bus.store.x("INSERT INTO revoked_keys(id, at) VALUES('k0123456789ab', 1)")  # revoked, row not yet gone
        self.assertEqual(self.refused("100.1.1.1", raw, self.sign(self.plan, raw)), "revoked")
        self.m.bus.store.x("DELETE FROM revoked_keys")
        self.m.bus.keys.remove_here("k0123456789ab")
        self.assertEqual(self.refused("100.1.1.1", raw, self.sign(self.plan, raw)), "revoked")
        self.assertEqual(self.m.squads.squad("sq1")["name"], "X")

    def test_still_owner_only(self):
        raw = self.body("X")
        with self.assertRaises(bus.BusError) as c:  # a valid signature does not make eve the owner
            self.write("100.7.7.7", raw, self.sign(self.plan, raw))
        self.assertIn("owner", str(c.exception))

    def test_over_http_the_exact_bytes_are_signed(self):
        self.m.serve(self)
        self.m.net.usermap["127.0.0.1"] = "nick@example.com"

        def send(raw, headers, query=""):  # http.client sends the target as given (urllib drops a #fragment)
            c = http.client.HTTPConnection("127.0.0.1", self.m.bus.port, timeout=10)
            self.addCleanup(c.close)
            c.request("PATCH", "/bus/v1/squads/sq1" + query, raw, {"X-Tailshare": "1", **headers})
            r = c.getresponse()
            return r.status, json.load(r)

        raw = self.body("Over HTTP")
        code, out = send(raw.replace(b", ", b",  "), self.sign(self.plan, raw))  # same JSON, other bytes
        self.assertEqual((code, out["error"], out["reason"]), (403, "signature", "bad_signature"))
        for query in ("?x", "?", "?tombstones=1", ";x", ";", "#x", "#"):  # not signed, so a write may not carry one
            code, out = send(raw, self.sign(self.plan, raw), query)
            self.assertEqual((code, out["error"], out["field"]), (400, "bad_request", "query"), query)
        code, out = send(raw, self.sign(self.plan, raw))
        self.assertEqual((code, out["squad"]["name"]), (200, "Over HTTP"))


if __name__ == "__main__":
    unittest.main()
