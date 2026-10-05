#!/usr/bin/env python3
"""Regression lab for kiwi-killswitchd — run it through tests/run.sh.

It loads the REAL daemon file, unmodified, inside a throwaway user + network
namespace and drives it for real: real nft, real routing, real sockets.

Guard: only `ip` and `nft` are actually executed by the daemon code, and they
act on the private namespace. nmcli / wg / resolvectl / systemctl are answered
from canned text, so nothing here can reach the host's NetworkManager,
resolved or systemd — and nothing here needs root.

Every test is named for the failure it guards against. Each of those failures
happened; see docs/TODO.md.
"""
import importlib.machinery
import importlib.util
import itertools
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "..", "daemon", "kiwi-killswitchd")
SCR = os.environ.get("LAB_DIR", "/nonexistent")


def load():
    ld = importlib.machinery.SourceFileLoader("ksd", DAEMON)
    spec = importlib.util.spec_from_loader("ksd", ld)
    m = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    ld.exec_module(m)
    return m


ksd = load()
ksd.STATE_DIR = SCR
ksd.CONFIG_JSON = SCR + "/config.json"
ksd.RUNTIME_RULESET = SCR + "/run.nft"
ksd.BOOT_RULESET = SCR + "/boot.nft"
ksd.nft_apply.__defaults__ = (ksd.RUNTIME_RULESET,)
LOG = []
ksd.log = LOG.append                     # keep the daemon's chatter out of the report

REAL = {"ip", "nft"}
FAKE = {"active": [], "all": [], "show": {}, "calls": []}
_real_run = ksd.run


def guarded(cmd, **kw):
    if cmd[0] in REAL:
        return _real_run(cmd, **kw)
    FAKE["calls"].append(cmd)
    out, rc = "", 0
    if cmd[0] == "nmcli":
        fields = cmd[cmd.index("-f") + 1] if "-f" in cmd else ""
        if "--active" in cmd:           # NAME,TYPE,DEVICE — tests may omit the device
            out = "\n".join(ln if ln.count(":") >= 2 else ln + ":" for ln in FAKE["active"])
        elif fields == "NAME,TYPE":
            out = "\n".join(FAKE["all"])
        else:
            out = FAKE["show"].get((fields, cmd[-1]), "")
    elif cmd[0] == "wg":
        rc = 1
    return subprocess.CompletedProcess(cmd, rc, out + ("\n" if out else ""), "")


ksd.run = guarded
Net = ksd.Net

# ---------------------------------------------------------------------------
RESULTS = {"ok": 0, "fail": 0}


def check(name, cond, detail=""):
    RESULTS["ok" if cond else "fail"] += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))
    return cond


def raises(fn, *a):
    try:
        fn(*a)
        return False
    except ValueError:
        return True


def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def fresh(**over):
    for suffix in ("", ".bak", ".tmp"):
        try:
            os.remove(ksd.CONFIG_JSON + suffix)
        except OSError:
            pass
    cfg = ksd.Config()
    for k, v in over.items():
        cfg[k] = v
    cfg.save()
    ks = ksd.KillSwitch(cfg, None)
    ks.notified = 0

    def _notify():
        ks.notified += 1
    ks._notify = _notify
    return cfg, ks


def tables():
    return sh("nft", "list", "tables")


def our_rules():
    """Policy rules at our priority, either family."""
    return [ln for ln in (sh("ip", "rule") + "\n" + sh("ip", "-6", "rule")).splitlines()
            if ln.startswith(f"{ksd.MGMT_RULE_PRIO}:")]


def chain(name):
    return sh("nft", "list", "chain", "inet", ksd.TABLE, name)


def mkev(exit="", tunnel=True, eps=(), hostnames=False, resolver="", dns_link="", node=None):
    ev = ksd.Eval()
    ev.exit_iface, ev.exit_is_tunnel, ev.complete = exit, tunnel, bool(exit)
    ev.endpoints, ev.hostnames = list(eps), hostnames
    ev.resolver, ev.dns_link, ev.node = resolver, dns_link, node
    return ev


def fake_physical(*names):
    """veth/dummy links have no sysfs `device`; let these stand in for real NICs."""
    orig = Net.is_physical
    Net.is_physical = staticmethod(lambda i: i in names or orig(i))


class Peer:
    """A second netns (the LAN / gateway / VPN server), joined by a veth."""

    def __init__(self):
        self.p = subprocess.Popen(["unshare", "-n", "sleep", "600"])
        time.sleep(0.4)
        self.pid = str(self.p.pid)

    def run(self, *cmd):
        r = subprocess.run(["nsenter", "-t", self.pid, "-n", *cmd],
                           capture_output=True, text=True)
        return (r.stdout + r.stderr).strip()

    def popen(self, *cmd):
        return subprocess.Popen(["nsenter", "-t", self.pid, "-n", *cmd],
                                stdout=subprocess.PIPE, text=True)

    def close(self):
        self.p.kill()


def link(peer, dev="lan0"):
    sh("ip", "link", "add", dev, "type", "veth", "peer", "name", "peer0")
    sh("ip", "link", "set", "peer0", "netns", peer.pid)
    sh("ip", "addr", "add", "10.9.0.1/24", "dev", dev)
    sh("ip", "-6", "addr", "add", "fd00:9::1/64", "dev", dev, "nodad")
    sh("ip", "link", "set", dev, "up")
    peer.run("ip", "link", "set", "lo", "up")
    peer.run("ip", "addr", "add", "10.9.0.2/24", "dev", "peer0")
    peer.run("ip", "addr", "add", "203.0.113.9/32", "dev", "peer0")
    peer.run("ip", "-6", "addr", "add", "fd00:9::2/64", "dev", "peer0", "nodad")
    peer.run("ip", "link", "set", "peer0", "up")
    peer.run("ip", "route", "add", "default", "via", "10.9.0.1")
    time.sleep(2.0)


RECV = r'''
import socket, select, sys, time
s4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s4.bind(("0.0.0.0", int(sys.argv[2])))
s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1); s6.bind(("::", int(sys.argv[2])))
end = time.time() + float(sys.argv[1]); got = set()
while time.time() < end:
    r, _, _ = select.select([s4, s6], [], [], 0.2)
    for s in r:
        d, a = s.recvfrom(100); got.add(d.decode())
print(" ".join(sorted(got)))
'''


def probe(peer, targets, secs=3.5, port=51820):
    """Send UDP to each (label, family, addr); return the labels the peer got."""
    rx = peer.popen("python3", "-c", RECV, str(secs), str(port))
    time.sleep(0.5)
    end = time.time() + secs - 1.2
    while time.time() < end:
        for label, fam, addr in targets:
            s = socket.socket(fam, socket.SOCK_DGRAM)
            try:
                s.sendto(label.encode(), (addr, port))
            except OSError:
                pass
            s.close()
        time.sleep(0.4)
    return set(rx.communicate()[0].split())


# ---------------------------------------------------------------------------
def t_validate():
    c = ksd.clean_setting
    for key, bad in (("allow_in_ports", "22 99999"), ("allow_in_ports", "0"),
                     ("allow_in_ports", "²"), ("allow_in_ports", "ssh"),
                     ("mgmt_subnets", "192.168.0.0/33"), ("bypass_subnets", "lan"),
                     ("bypass_subnets", "fe80::/64%eth0"),
                     ("dns_custom", "fe80::1%x; accept #"), ("dns_custom", "9.9.9.9\naccept"),
                     ("doh_servers", "dns.example"), ("doh_servers", "1.1.1.1#bad name"),
                     ("doh_path", "/x y"), ("doh_path", "dns-query"),
                     ("mode", "open"), ("dns_mode", "any"), ("hardening", "max"),
                     ("auto_node", "maybe"), ("hardening_confirm_sec", "abc"),
                     ("hardening_confirm_sec", "1"), ("deadman_sec", "-5"),
                     ("vpn", "--help"), ("vpn", "a\nb"), ("dns_search", "~bad domain;")):
        check(f"rejects {key}={bad!r}", raises(c, key, bad))
    for key, val, want in (
            ("allow_in_ports", "22, 8022 22", "22 8022"),
            ("mgmt_subnets", "192.168.0.0/255.255.0.0", "192.168.0.0/16"),
            ("mgmt_subnets", "192.168.7.104/16 fd00::1/8", "192.168.0.0/16 fd00::/8"),
            ("dns_custom", "2620:FE::FE", "2620:fe::fe"),
            ("auto_node", "Yes", "1"), ("allow_lan", "", "0"),
            ("doh_servers", "1.1.1.1 203.0.113.5#doh.example", "1.1.1.1 203.0.113.5#doh.example"),
            ("doh_path", "", "/dns-query"), ("dns_search", "~kiwi ~.", "~kiwi ~."),
            ("vpn", "My VPN (home)", "My VPN (home)"), ("deadman_sec", "0", "0")):
        got = c(key, val)
        check(f"normalises {key}={val!r}", got == want, got)
    n = ksd.clean_node
    good = {"name": "m1", "ip": "192.168.50.1", "mac": "AA-BB-CC-00-11-22", "dns": "192.168.50.1"}
    check("node: MAC is normalised", n(good)["mac"] == "aa:bb:cc:00:11:22")
    for field, bad in (("dns", "9.9.9.9 accept\naccept"), ("dns", "fe80::1%eth0"),
                       ("ip", ""), ("ip", "gateway"), ("mac", "aa:bb"), ("name", "")):
        check(f"node: rejects {field}={bad!r}", raises(n, dict(good, **{field: bad})))
    check("ports_of drops what nft would reject",
          ksd.ports_of("22 99999 ² x 0 8022") == ["22", "8022"])
    check("subnets_of drops junk, canonicalises the rest",
          ksd.subnets_of("10.0.0.5/8 nope 10.0.0.0/255.0.0.0") == ["10.0.0.0/8"])
    check("valid_iface refuses quotes and wildcards",
          not ksd.valid_iface('x"};accept#') and not ksd.valid_iface("tun*")
          and ksd.valid_iface("id-bluefin-dev") and ksd.valid_iface("enp3s0.10"))


def t_rulesets():
    """Every shape of ruleset must actually load — one bad line loses them all."""
    sh("ip", "link", "add", "nic0", "type", "dummy")
    sh("ip", "link", "add", "nic1", "type", "dummy")
    fake_physical("nic0", "nic1")
    Net.lan_subnets = staticmethod(lambda: ["192.168.50.0/24"])
    node = {"name": "m1", "ip": "192.168.50.1", "mac": "", "dns": "192.168.50.1"}
    exits = (("none", {}), ("tunnel", {"exit": "wg9"}),
             ("node", {"exit": "nic0", "tunnel": False, "node": node}))
    endpoints = ([], [("198.51.100.7", "udp", "51820")],
                 [("198.51.100.7", "tcp", "443"), ("198.51.100.8", "tcp", "80"),
                  ("2001:db8::7", "udp", "51820"), ("203.0.113.1", "", "")])
    dns = (("", ""), ("10.8.0.1", "EXIT"), ("9.9.9.9", "nic0"), ("2620:fe::fe", "nic0"))
    extras = ({}, {"allow_lan": True, "strict_input": True, "allow_in_ports": "22 8022",
                   "bypass_subnets": "10.20.0.0/16 fd00:20::/64",
                   "mgmt_subnets": "192.168.0.0/16 fd00:99::/64"})
    n = bad = 0
    for hard, (ename, ekw), eps, (res, rlink), extra in itertools.product(
            ksd.HARDENING_LEVELS, exits, endpoints, dns, extras):
        cfg, ks = fresh(hardening=hard, **extra)
        ev = mkev(eps=eps, hostnames=bool(extra), resolver=res,
                  dns_link=(ekw.get("exit", "") if rlink == "EXIT" else rlink), **ekw)
        n += 1
        if not ksd.nft_apply(ks.fw.full(ev, cfg.settings(), ["1.1.1.1", "2606:4700:4700::1111"])):
            bad += 1
            print("    rejected:", hard, ename, eps, res, extra, LOG[-1][:200])
    check(f"all {n} full rulesets load", bad == 0, f"{bad} rejected")
    cfg, ks = fresh()
    for name, text in (("node mode, with a resolver", ks.fw.node(mkev(resolver="192.168.50.1"), {})),
                       ("node mode, no resolver", ks.fw.node(mkev(), {})),
                       ("hard block", ks.fw.hard_block({"mgmt_subnets": "192.168.0.0/16 fd00::/8",
                                                         "allow_in_ports": "22"})),
                       ("hard block from nothing", ks.fw.hard_block(None)),
                       ("bare lockdown", ks.fw.lockdown())):
        check(f"{name} loads", ksd.nft_apply(text))

    # ---- what the rules say ----
    p = fresh()[0].settings()
    fw = ksd.Firewall(None)
    wg = [("198.51.100.7", "udp", "51820")]
    down = fw.full(mkev(eps=wg), p, ["1.1.1.1"])
    check("endpoint rule is limited to the tunnel's own protocol and port",
          'ip daddr 198.51.100.7 udp dport 51820 accept comment "vpn endpoint"' in down)
    check("an endpoint with no known port still gets through (address only)",
          'ip daddr 203.0.113.1 accept comment "vpn endpoint"'
          in fw.full(mkev(eps=[("203.0.113.1", "", "")]), p, []))
    check("no DoH hole when every endpoint is a literal address", "doh bootstrap" not in down)
    named = fw.full(mkev(eps=wg, hostnames=True), p, ["1.1.1.1"])
    check("DoH hole is root-only when a hostname endpoint needs it",
          'meta skuid 0 ip daddr 1.1.1.1 tcp dport 443 accept comment "doh bootstrap"' in named)
    check("...and closes again once the tunnel carries traffic",
          "doh bootstrap" not in fw.full(mkev(exit="wg9", eps=wg, hostnames=True), p, ["1.1.1.1"]))
    up = fw.full(mkev(exit="wg9", eps=wg, resolver="10.8.0.1", dns_link="wg9"), p, [])
    check("a resolver reached through the tunnel gets no address rule of its own",
          '"resolver"' not in up)
    check("a resolver reached outside the tunnel does",
          'ip daddr 9.9.9.9 udp dport 53 accept comment "resolver"'
          in fw.full(mkev(resolver="9.9.9.9", dns_link="nic0"), p, []))
    check("neighbour discovery is allowed out (IPv6 has no ARP)",
          "nd-neighbor-solicit" in down.split("chain input")[0])
    check("an unusable interface name never becomes an exit",
          'oifname' not in fw.full(mkev(exit='x"};accept#'), p, []).replace('oifname "lo"', "")
          .replace('oifname { "lo" }', ""))
    old = dict(p, allow_in_ports="22 99999 ²", mgmt_subnets="192.168.0.0/255.255.0.0 junk",
               bypass_subnets="10.0.0.9/8", hardening="max")
    legacy = fw.full(mkev(), old, [])
    check("a config written before validation existed is sanitised on the way out",
          "99999" not in legacy and "255.255.0.0" not in legacy
          and "ip daddr 192.168.0.0/16 accept" in legacy and "ip daddr 10.0.0.0/8 accept" in legacy
          and "chain forward" in legacy and ksd.nft_apply(legacy))


def t_failclosed():
    cfg, ks = fresh(allow_in_ports="22 99999", mgmt_subnets="192.168.0.0/255.255.0.0")
    ks.arm()
    check("a bad port / netmask already in the config no longer costs the table",
          "kiwi_ks" in tables() and ks.status["state"] == "blocking", ks.status["detail"])

    cfg, ks = fresh()
    ks.fw.full = lambda *a: "table inet kiwi_ks { this is not nft }\n"
    ks.arm()
    check("ruleset rejected -> hard block is loaded, status says blocking",
          "policy drop" in chain("output") and ks.status["enforcing"]
          and ks.status["state"] == "blocking")
    ksd.nft_drop_tables()
    ks._loaded = None
    ks.fw.hard_block = lambda *a: "garbage\n"
    ks.arm()
    check("hard block rejected too -> the bare lockdown still blocks",
          "policy drop" in chain("output") and "mgmt" not in chain("output")
          and ks.status["enforcing"])
    ksd.nft_drop_tables()
    ks._loaded = None
    ks.fw.lockdown = lambda: "garbage\n"
    ks.arm()
    check("nothing loads at all -> status is NOT PROTECTED, never 'armed and fine'",
          not ks.status["enforcing"] and ks.status["state"] == "unprotected")

    cfg, ks = fresh()
    orig = ksd.Situation.evaluate
    ksd.Situation.evaluate = lambda self: 1 / 0
    try:
        ks.arm()
    except Exception as e:  # noqa: BLE001
        check("an exception inside apply() does not escape", False, repr(e))
    ksd.Situation.evaluate = orig
    check("...it ends in a loaded block and an honest status",
          "policy drop" in chain("output") and ks.status["armed"]
          and ks.status["state"] == "blocking", str(ks.status))

    ksd.nft_drop_tables()
    FAKE["all"] = ["wg-home:wireguard"]
    FAKE["show"][("wireguard.peers", "wg-home")] = \
        "wireguard.peers:AbC= allowed-ips=0.0.0.0/0 endpoint=vpn..example.com:51820"
    cfg, ks = fresh(vpn="wg-home", doh_servers="")
    ks.arm()
    check("a malformed endpoint hostname is survived (it used to raise out of arm)",
          ks.status["armed"] and "kiwi_ks" in tables(), str(ks.status))
    FAKE["all"], FAKE["show"] = [], {}

    def boom(*a):
        raise RuntimeError("resolved is on fire")
    cfg, ks = fresh()
    ks.dns.apply = boom
    ks.arm()
    check("a failing DNS step does not tear down a good ruleset",
          ks.status["enforcing"] and "ks-drop-out" in chain("output")
          and any("dns step failed" in line for line in LOG))

    # ---- the config file ----
    ksd.nft_drop_tables()
    cfg, ks = fresh(armed=True, mgmt_subnets="192.168.0.0/16")
    with open(ksd.CONFIG_JSON, "w") as fh:
        fh.write('{"armed": true, "vpn": "x", "mgmt_sub')          # torn write
    c2 = ksd.Config()
    check("unreadable config, no good copy -> treated as ARMED",
          c2.unreadable and c2["armed"] is True)
    check("...and the boot unit blocks", ksd.restore_only() == 0 and "kiwi_ks" in tables())
    ks2 = ksd.KillSwitch(c2, None)
    ks2.restore()
    check("...and the daemon explains why",
          "could not be read" in ks2.status["detail"], ks2.status["detail"])

    cfg, ks = fresh(armed=True, mgmt_subnets="192.168.0.0/16")
    cfg["vpn"] = "second-save"
    cfg.save()                                 # now a .bak of the first save exists
    with open(ksd.CONFIG_JSON, "w") as fh:
        fh.write("")
    c3 = ksd.Config()
    check("unreadable config WITH a good copy -> the copy is used",
          not c3.unreadable and c3["armed"] is True and c3["mgmt_subnets"] == "192.168.0.0/16")
    for suffix in ("", ".bak"):
        os.remove(ksd.CONFIG_JSON + suffix)
    check("no config at all is a fresh install: disarmed",
          ksd.Config()["armed"] is False and not ksd.Config().unreadable)

    ksd.nft_drop_tables()
    cfg, ks = fresh(armed=True)
    orig_hb = ksd.Firewall.hard_block
    ksd.Firewall.hard_block = lambda self, p: "garbage\n"
    rc = ksd.restore_only()
    ksd.Firewall.hard_block = orig_hb
    check("boot: hard block rejected -> lockdown, not an open network",
          rc == 0 and "policy drop" in chain("output"))
    check("saving never raises, even with nowhere to write",
          _save_to_nowhere() is False)


def _save_to_nowhere():
    keep = ksd.STATE_DIR, ksd.CONFIG_JSON
    ksd.STATE_DIR, ksd.CONFIG_JSON = "/proc/nope", "/proc/nope/config.json"
    try:
        return ksd.Config().save()
    finally:
        ksd.STATE_DIR, ksd.CONFIG_JSON = keep


def t_inject():
    cfg, ks = fresh()
    svc = ksd.Service.__new__(ksd.Service)
    svc.cfg, svc.ks, svc.conn = cfg, ks, None
    payload = '9.9.9.9 udp dport 53 accept\n    accept comment "INJECTED"\n    ip daddr 9.9.9.9'
    v = ksd.GLib.Variant("(a{ss})", ({"name": "evil", "ip": "10.9.0.2", "mac": "", "dns": payload},))
    check("WriteNode refuses a resolver that is not an address",
          raises(svc._dispatch, "WriteNode", v) and cfg["nodes"] == [])
    # a config from before the check existed
    cfg["nodes"] = [{"name": "evil", "ip": "10.9.0.2", "mac": "", "dns": payload}]
    cfg["dns_mode"] = "system"
    ev = mkev(exit="nic0", tunnel=False, node=cfg["nodes"][0])
    ksd.Situation(cfg, ks.resolver)._pick_dns(ev)
    text = ks.fw.full(ev, cfg.settings(), []) + ks.fw.node(ev, cfg.settings())
    check("...and one already stored never reaches a ruleset",
          "INJECTED" not in text and ev.resolver == "" and ev.dns_path == "blocked")
    check("Arm refuses a connection name that reads as an option",
          raises(svc._dispatch, "Arm", ksd.GLib.Variant("(s)", ("--help",))))
    check("SetPending refuses a bad value and stages nothing",
          raises(svc._dispatch, "SetPending", ksd.GLib.Variant("(ss)", ("allow_in_ports", "22 99999")))
          and cfg["pending"] == {})


def t_mgmt(phase="1"):
    if phase == "reboot":
        cfg = ksd.Config()
        ks = ksd.KillSwitch(cfg, None)
        ks._sync_mgmt(cfg.settings())
        print("RULE" if " to 192.168.0.0/16 lookup main" in sh("ip", "rule") else "MISSING")
        return
    rule = lambda: " to 192.168.0.0/16 lookup main" in sh("ip", "rule")   # noqa: E731
    cfg, ks = fresh(mgmt_subnets="192.168.0.0/16 fd00:99::/64")
    p = cfg.settings()
    ks._sync_mgmt(p)
    check("first apply installs the policy rule (v4 and v6)",
          rule() and "fd00:99::/64 lookup main" in sh("ip", "-6", "rule"))
    saves = os.stat(ksd.CONFIG_JSON).st_mtime_ns
    ks._sync_mgmt(p)
    check("an unchanged re-apply adds nothing and writes nothing",
          sh("ip", "rule").count("lookup main") == 2
          and os.stat(ksd.CONFIG_JSON).st_mtime_ns == saves)
    sh("ip", "rule", "del", "to", "192.168.0.0/16", "lookup", "main", "priority", "90")
    ks._sync_mgmt(p)
    check("a rule that vanished is put back on the next apply", rule())
    out = subprocess.run(["unshare", "-n", sys.executable, "-B", __file__, "_mgmt_reboot"],
                         capture_output=True, text=True).stdout
    check("...and after a reboot while armed (same config, fresh kernel)", "RULE" in out, out)
    cfg["mgmt_subnets"] = "fd00:99::/64"
    ks._sync_mgmt(cfg.settings())
    check("a subnet taken out of the setting loses its rule", not rule())
    ks._del_mgmt_routes()
    check("disarm removes everything we own",
          our_rules() == [] and cfg["runtime"]["mgmt_routes"] == [], str(our_rules()))

    # ---- the off-link pin (an OpenVPN-style tunnel that takes over `main`) ----
    peer = Peer()
    try:
        link(peer)
        fake_physical("lan0")
        sh("ip", "route", "add", "default", "via", "10.9.0.2", "dev", "lan0")
        if "Error" in sh("ip", "tuntap", "add", "dev", "tun9", "mode", "tun") or not Net.is_tunnel("tun9"):
            print("  skip  (no tun device available here — pin tests not run)")
            return
        sh("ip", "addr", "add", "10.77.0.2/24", "dev", "tun9")
        sh("ip", "link", "set", "tun9", "up")
        sh("ip", "route", "add", "0.0.0.0/1", "dev", "tun9")
        sh("ip", "route", "add", "128.0.0.0/1", "dev", "tun9")
        cfg, ks = fresh(mgmt_subnets="172.31.0.0/16 10.9.0.0/24")
        ks._sync_mgmt(cfg.settings())
        dev = lambda a: Net.route_dev(a)                                 # noqa: E731
        check("an off-link admin subnet is pinned to the physical uplink",
              dev("172.31.5.5") == "lan0" and dev("1.1.1.1") == "tun9", sh("ip", "route"))
        check("an attached subnet is never pinned through the gateway",
              "10.9.0.0/24 via" not in sh("ip", "route"))
        sh("ip", "route", "del", "172.31.0.0/16")
        ks._sync_mgmt(cfg.settings())
        check("a pin that vanished is put back", dev("172.31.5.5") == "lan0")
        before = sh("ip", "route")
        ks._sync_mgmt(cfg.settings())
        check("...and a held pin is left alone (no route churn, no event loop)",
              sh("ip", "route") == before)
        ks._del_mgmt_routes()
        check("the pin is deleted exactly — the connected route survives",
              "172.31.0.0/16" not in sh("ip", "route") and "10.9.0.0/24 dev lan0" in sh("ip", "route"))
    finally:
        peer.close()


def t_net6():
    peer = Peer()
    try:
        link(peer)
        cfg, ks = fresh()
        ev = mkev(eps=[("10.9.0.2", "udp", "51820"), ("fd00:9::2", "udp", "51820")])
        check("ruleset loads", ksd.nft_apply(ks.fw.full(ev, cfg.settings(), [])))
        sh("ip", "neigh", "flush", "dev", "lan0")
        sh("ip", "-6", "neigh", "flush", "dev", "lan0")
        got = probe(peer, [("v4", socket.AF_INET, "10.9.0.2"), ("v6", socket.AF_INET6, "fd00:9::2")])
        check("armed, tunnel down: the handshake reaches an IPv4 endpoint", "v4" in got)
        check("...and an IPv6 endpoint (neighbour discovery used to be dropped)", "v6" in got)
        got = probe(peer, [("other-port", socket.AF_INET, "10.9.0.2")], port=8080)
        check("...but nothing else on that host: another port stays blocked", not got, str(got))
        got = probe(peer, [("elsewhere", socket.AF_INET, "203.0.113.9")])
        check("...and nothing to any other address", not got, str(got))
    finally:
        peer.close()


def t_arp():
    peer = Peer()
    try:
        link(peer)
        real = peer.run("ip", "-o", "link", "show", "peer0").split("link/ether ")[1].split()[0]
        cfg, ks = fresh()
        check("gateway MAC is learned with no firewall", Net.gw_mac("10.9.0.2", "lan0") == real)
        for name, text in (("the boot hard block", ks.fw.hard_block(cfg.settings())),
                           ("the bare lockdown", ks.fw.lockdown()),
                           ("a full ruleset with no exit", ks.fw.full(mkev(), cfg.settings(), []))):
            ksd.nft_apply(text)
            sh("ip", "neigh", "flush", "dev", "lan0")
            check(f"...and behind {name} (ping + neighbour table could not)",
                  Net.gw_mac("10.9.0.2", "lan0") == real)
        check("an address nobody answers for gives no MAC, within a second",
              _timed(lambda: Net.arp_mac("10.9.0.77", "lan0")) < 1.5
              and Net.arp_mac("10.9.0.77", "lan0") == "")
        # the trusted-node decision end to end, behind the hard block
        fake_physical("lan0")
        sh("ip", "route", "add", "default", "via", "10.9.0.2", "dev", "lan0")
        cfg, ks = fresh(trusted_node="m1")
        cfg["nodes"] = [{"name": "m1", "ip": "10.9.0.2", "mac": real, "dns": "10.9.0.2"}]
        ksd.nft_apply(ks.fw.hard_block(cfg.settings()))
        sh("ip", "neigh", "flush", "dev", "lan0")
        ks.arm()
        check("a MAC-bound node is recognised coming up from the boot block",
              ks.status["state"] == "trusted-node", ks.status["detail"])
        cfg["nodes"][0]["mac"] = "02:00:00:00:00:01"
        ks.sync()
        check("...and a wrong MAC is still refused", ks.status["state"] == "blocking")
    finally:
        peer.close()


def _timed(fn):
    t = time.monotonic()
    fn()
    return time.monotonic() - t


def t_classify():
    peer = Peer()
    try:
        link(peer)
        sh("ip", "link", "add", "br0", "type", "bridge")
        sh("ip", "addr", "flush", "dev", "lan0")
        sh("ip", "link", "set", "lan0", "master", "br0")
        sh("ip", "addr", "add", "10.9.0.1/24", "dev", "br0")
        sh("ip", "link", "set", "br0", "up")
        sh("ip", "link", "add", "dum0", "type", "dummy")
        time.sleep(1.5)
        sh("ip", "route", "add", "default", "via", "10.9.0.2", "dev", "br0")
        if not os.path.isdir("/sys/class/net/br0"):
            print("  skip  (sysfs does not show this namespace)")
            return
        check("a bridge is not a tunnel", not Net.is_tunnel("br0"))
        check("a bridge port is not a tunnel", not Net.is_tunnel("lan0"))
        check("a dummy link is not a tunnel", not Net.is_tunnel("dum0"))
        have_tun = "Error" not in sh("ip", "tuntap", "add", "dev", "tun9", "mode", "tun")
        if have_tun:
            check("a tun device is a tunnel", Net.is_tunnel("tun9"))
        if "Error" not in sh("ip", "link", "add", "wg9", "type", "wireguard"):
            check("a WireGuard device is a tunnel", Net.is_tunnel("wg9"))
        check("with no real NIC under it, a bridge is not an uplink either (virbr0)",
              not Net.is_uplink("br0"))
        fake_physical("lan0")
        check("a bridge over a real NIC is an uplink", Net.is_uplink("br0")
              and Net.physical_under("br0") == {"lan0"})
        check("...which the gateway logic can now see", Net.physical_gw() == ("10.9.0.2", "br0"))

        # NetworkManager reports the BASE device for a plugin VPN that has no
        # address yet. On a bridged uplink that used to become the exit.
        FAKE["active"] = FAKE["all"] = ["corp-ovpn:vpn"]
        FAKE["show"][("GENERAL.IP-IFACE", "corp-ovpn")] = "GENERAL.IP-IFACE:br0"
        FAKE["show"][("vpn.data", "corp-ovpn")] = \
            "vpn.data:dev = tun, proto-tcp = yes, remote = 198.51.100.7:443\\, 198.51.100.8"
        cfg, ks = fresh(vpn="corp-ovpn")
        ks.arm()
        check("an activating VPN on a bridged uplink does not turn the LAN into the exit",
              ks.status["exit_iface"] == "" and ks.status["state"] == "blocking", str(ks.status))
        got = probe(peer, [("plain", socket.AF_INET, "203.0.113.9")])
        check("...so ordinary traffic stays inside", not got, str(got))
        out = chain("output")
        check("OpenVPN remotes are parsed with their own port and the global protocol",
              "198.51.100.7 tcp dport 443" in out and "198.51.100.8 tcp dport 1194" in out, out)
        FAKE["active"], FAKE["all"], FAKE["show"] = [], [], {}

        # allow_lan
        sh("ip", "link", "add", "wlan9", "type", "dummy")
        sh("ip", "addr", "add", "11.0.0.5/1", "dev", "wlan9")
        sh("ip", "addr", "add", "192.168.77.5/24", "dev", "wlan9")
        sh("ip", "link", "set", "wlan9", "up")
        fake_physical("lan0", "wlan9")
        check("allow_lan takes private ranges only (a /1 'LAN' is not a LAN)",
              Net.lan_subnets() == ["10.9.0.0/24", "192.168.77.0/24"], str(Net.lan_subnets()))
    finally:
        peer.close()


def t_endpoints():
    sp = Net._split_hostport
    for tok, want in (("198.51.100.44:51820", ("198.51.100.44", "51820", "")),
                      ("198.51.100.44\\:51820", ("198.51.100.44", "51820", "")),
                      ("[2001:db8::1]:51820", ("2001:db8::1", "51820", "")),
                      ("2001:db8::1", ("2001:db8::1", "", "")),
                      ("vpn.example.com", ("vpn.example.com", "", "")),
                      ("vpn.example.com:443:tcp", ("vpn.example.com", "443", "tcp")),
                      (" 203.0.113.52:53", ("203.0.113.52", "53", ""))):
        check(f"endpoint {tok!r}", sp(tok) == want, str(sp(tok)))
    FAKE["show"][("wireguard.peers", "wg")] = ("wireguard.peers:KEY= allowed-ips=0.0.0.0/0;::/0 "
                                               "endpoint=198.51.100.44:51820 persistent-keepalive=25")
    check("WireGuard: saved peer -> udp/port",
          Net.static_endpoints("wg") == [("198.51.100.44", "udp", "51820")])
    FAKE["show"][("vpn.data", "ov")] = (
        "vpn.data:ca = /x/ca.pem, cipher = AES-256-GCM, dev = tun, proto-tcp = yes, "
        "remote = 203.0.113.52:53\\, 203.0.113.52:80\\, 203.0.113.108:1194, ta-dir = 1")
    check("OpenVPN: every remote with its port, protocol from proto-tcp",
          Net.static_endpoints("ov") == [("203.0.113.52", "tcp", "53"), ("203.0.113.52", "tcp", "80"),
                                         ("203.0.113.108", "tcp", "1194")], str(Net.static_endpoints("ov")))
    FAKE["show"][("vpn.data", "ov2")] = "vpn.data:port = 443, remote = a.example:1194:udp\\, b.example"
    check("OpenVPN: per-remote protocol, default port from `port`",
          Net.static_endpoints("ov2") == [("a.example", "udp", "1194"), ("b.example", "udp", "443")])
    FAKE["show"][("vpn.data", "ov3")] = ("vpn.data:proxy-port = 3128, proxy-server = 10.1.1.1, "
                                         "proxy-type = http, remote = far.example:443")
    check("OpenVPN through a proxy: the proxy is the endpoint",
          Net.static_endpoints("ov3") == [("10.1.1.1", "tcp", "3128")])
    FAKE["show"] = {}


def t_nm():
    """How NetworkManager's listings are read."""
    FAKE["active"] = [r"home\: wg:wireguard:wg0"]
    FAKE["all"] = [r"home\: wg:wireguard", "plain:802-3-ethernet"]
    check("a connection name containing a colon is read whole",
          Net.active_vpns() == ["home: wg"] and Net.conn_active("home: wg")
          and Net.all_vpns() == [("home: wg", "wireguard", "yes")], str(Net.all_vpns()))

    if "Error" in sh("ip", "tuntap", "add", "dev", "tun0", "mode", "tun"):
        print("  skip  (no tun device available here)")
        return
    sh("ip", "tuntap", "add", "dev", "tun7", "mode", "tun")
    sh("ip", "addr", "add", "10.41.0.5/21", "dev", "tun0")
    # what NM really lists with OpenVPN up: the connection, and its tun device
    FAKE["active"] = ["corp-ovpn:vpn:eth0", "tun0:tun:tun0", "Wired:802-3-ethernet:eth0"]
    FAKE["all"] = ["corp-ovpn:vpn", "tun0:tun", "other-wg:wireguard", "Wired:802-3-ethernet"]
    FAKE["show"][("IP4.ADDRESS,IP6.ADDRESS,IP4.GATEWAY", "corp-ovpn")] = \
        "IP4.ADDRESS[1]:10.41.0.5/21\nIP4.GATEWAY:10.41.0.1"
    check("a plugin VPN's own tun device is not a second VPN",
          Net.active_vpns() == ["corp-ovpn"], str(Net.active_vpns()))
    check("...and is not offered as a connection to protect",
          [v[0] for v in Net.all_vpns()] == ["corp-ovpn", "other-wg"], str(Net.all_vpns()))
    cfg, ks = fresh(vpn="corp-ovpn")
    ev = ksd.Situation(cfg, ks.resolver).evaluate()
    check("...so the protected VPN does not conflict with itself", ev.conflicts == [], str(ev.conflicts))
    FAKE["active"].append("tun7:tun:tun7")
    FAKE["all"].append("tun7:tun")
    check("a tun started outside NetworkManager still counts as another tunnel",
          Net.active_vpns() == ["corp-ovpn", "tun7"]
          and ksd.Situation(cfg, ks.resolver).evaluate().conflicts == ["tun7"])
    cfg, ks = fresh(vpn="tun7")
    check("...and can itself be the protected one", Net.conn_active("tun7")
          and "tun7" in [v[0] for v in Net.all_vpns()])
    FAKE["active"], FAKE["all"], FAKE["show"] = [], [], {}


def t_fwd():
    lan, guest = Peer(), Peer()
    try:
        link(lan)
        sh("ip", "link", "add", "vm0", "type", "veth", "peer", "name", "g0")
        sh("ip", "link", "set", "g0", "netns", guest.pid)
        sh("ip", "addr", "add", "10.88.0.1/24", "dev", "vm0")
        sh("ip", "link", "set", "vm0", "up")
        guest.run("ip", "link", "set", "lo", "up")
        guest.run("ip", "addr", "add", "10.88.0.2/24", "dev", "g0")
        guest.run("ip", "link", "set", "g0", "up")
        guest.run("ip", "route", "add", "default", "via", "10.88.0.1")
        with open("/proc/sys/net/ipv4/ip_forward", "w") as fh:
            fh.write("1")
        time.sleep(1.5)
        ping = lambda dst: "1 received" in guest.run("ping", "-c1", "-W2", dst)   # noqa: E731
        check("baseline: the guest reaches the LAN host", ping("10.9.0.2"))
        cfg, ks = fresh(bypass_subnets="10.9.0.0/24")
        ksd.nft_apply(ks.fw.full(mkev(), cfg.settings(), []))
        check("strict: a guest can use an allowed LAN, replies included", ping("10.9.0.2"))
        check("...and still cannot reach anything else", not ping("203.0.113.9"))
        lan_to_guest = "1 received" in lan.run("ping", "-c1", "-W2", "10.88.0.2")
        check("...and the LAN cannot open a connection INTO the guest", not lan_to_guest)
    finally:
        lan.close()
        guest.close()


def t_churn():
    sh("ip", "link", "add", "nic0", "type", "dummy")
    sh("ip", "link", "set", "nic0", "up")
    sh("ip", "addr", "add", "192.168.60.5/24", "dev", "nic0", "valid_lft", "300", "preferred_lft", "200")
    raw1 = sh("ip", "-o", "addr", "show")
    fp1 = Net.fingerprint()
    time.sleep(2.2)
    check("a DHCP-style lease really does tick in `ip addr`", raw1 != sh("ip", "-o", "addr", "show"))
    check("the fingerprint ignores the countdown", fp1 == Net.fingerprint())
    sh("ip", "addr", "add", "192.168.61.5/24", "dev", "nic0")
    check("...and still sees a real change", fp1 != Net.fingerprint())

    sh("ip", "route", "add", "default", "dev", "nic0")       # so packets get as far as the hook
    cfg, ks = fresh(mgmt_subnets="192.168.60.0/24")
    ks.arm()
    drops = lambda: int(chain("output").split("ks-drop-out")[0].rsplit("packets", 1)[1].split()[0])   # noqa: E731
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(3):
        try:
            s.sendto(b"x", ("198.51.100.1", 9))
        except OSError:
            pass
    n = drops()
    check("blocked packets are counted", n >= 3, str(n))
    flushes = lambda: sum(1 for c in FAKE["calls"] if c[:2] == ["resolvectl", "flush-caches"])   # noqa: E731
    loads = sum(1 for line in LOG if line.startswith("applied"))
    notes, fl = ks.notified, flushes()
    for _ in range(3):
        ks.sync()
    check("re-syncing an unchanged world keeps the drop counters", drops() >= n, str(drops()))
    check("...signals no front-end", ks.notified == notes)
    check("...flushes no DNS cache", flushes() == fl)
    check("...and logs nothing", sum(1 for line in LOG if line.startswith("applied")) == loads)
    sh("nft", "delete", "table", "inet", ksd.TABLE)
    ks.sync()
    check("but a table that vanished is reloaded", "kiwi_ks" in tables())

    # the resolver path: flush once per change
    FAKE["calls"].clear()
    ev = mkev(exit="wg9", resolver="10.8.0.1", dns_link="wg9")
    ks.dns.apply(ev, cfg.settings())
    ks.dns.apply(ev, cfg.settings())
    check("DNS steering is re-asserted every time, the cache flushed once", flushes() == 1
          and sum(1 for c in FAKE["calls"] if c[:3] == ["resolvectl", "dns", "wg9"]) == 2)
    ev.resolver = "10.8.0.53"
    ks.dns.apply(ev, cfg.settings())
    check("...and again when the resolver actually changes", flushes() == 2)

    # the resolver TTL
    cfg, ks = fresh()
    cfg["endpoint_cache"]["vpn.example.com"] = ["198.51.100.7"]
    asked = []
    ks.resolver._doh_query = lambda *a: asked.append(a) or ["198.51.100.7"]
    for _ in range(5):
        ks.resolver.resolve("vpn.example.com")
    check("a hostname endpoint is not re-resolved on every apply", len(asked) == 2, str(len(asked)))


def t_monitor():
    from gi.repository import GLib
    loop = GLib.MainLoop()
    seen = []
    deb = ksd.Debounced(lambda: seen.append(time.monotonic()))
    mon = ksd.LinkMonitor(deb.poke)
    GLib.timeout_add(300, lambda: sh("ip", "link", "add", "ev0", "type", "dummy") and False)
    GLib.timeout_add(2600, lambda: mon._proc.kill() or False)
    GLib.timeout_add(5600, lambda: sh("ip", "link", "add", "ev1", "type", "dummy") and False)
    GLib.timeout_add(8000, loop.quit)
    loop.run()
    check("a kernel link event reaches the handler (debounced)", len(seen) >= 1)
    check("the monitor restarts after it dies", any("ip monitor ended" in line for line in LOG))
    check("...and delivers events again", len(seen) >= 2, str(len(seen)))

    seen.clear()
    t0 = time.monotonic()
    tick = GLib.timeout_add(400, lambda: deb.poke() or True)      # a link that never settles
    GLib.timeout_add(9000, loop.quit)
    loop.run()
    GLib.source_remove(tick)
    check("a continuous stream of events cannot postpone the sync for ever",
          bool(seen) and seen[0] - t0 < 7.5, str([round(x - t0, 1) for x in seen[:3]]))


def t_dbus():
    """The real Service, on a private session bus, driven like a client would."""
    child = subprocess.Popen([sys.executable, "-B", __file__, "_serve"])
    dest, obj = ksd.BUS_NAME, ksd.OBJ_PATH

    def call(method, *args):
        r = subprocess.run(["gdbus", "call", "--session", "-d", dest, "-o", obj,
                            "-m", f"{dest}.{method}", *args], capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr).strip()

    try:
        for _ in range(50):
            if call("GetStatus")[0] == 0:
                break
            time.sleep(0.2)
        rc, out = call("GetStatus")
        check("GetStatus answers and carries `state`", rc == 0 and "'state': <'off'>" in out, out)
        rc, out = call("SetPending", "allow_in_ports", "22 99999")
        check("a bad value comes back as a D-Bus error with a readable reason",
              rc != 0 and "not a port" in out, out)
        rc, out = call("SetPending", "mgmt_subnets", "192.168.0.0/255.255.0.0")
        check("a netmask is accepted and stored as CIDR",
              rc == 0 and "'mgmt_subnets': '192.168.0.0/16'" in call("GetPending")[1])
        check("Apply promotes it", call("Apply")[0] == 0
              and "'mgmt_subnets': '192.168.0.0/16'" in call("GetConfig")[1])
        rc, out = call("WriteNode", "{'name': 'n', 'ip': '10.0.0.1', 'mac': '', 'dns': '1.1.1.1 accept'}")
        check("WriteNode refuses a resolver that is not an address", rc != 0 and "not an IP" in out, out)
        check("WriteNode accepts a good one", call(
            "WriteNode", "{'name': 'n', 'ip': '10.0.0.1', 'mac': 'AA:BB:CC:00:11:22', 'dns': '10.0.0.1'}")[0] == 0
            and "'mac': 'aa:bb:cc:00:11:22'" in call("ListNodes")[1])
        check("Arm refuses a name that reads as an option", call("Arm", "--help")[0] != 0)
        check("Arm works", call("Arm", "")[0] == 0)
        rc, out = call("GetStatus")
        check("...and reports blocking, enforcing", "'state': <'blocking'>" in out
              and "'enforcing': <true>" in out and "kiwi_ks" in tables(), out)
        sh("nft", "delete", "table", "inet", ksd.TABLE)
        rc, out = call("GetStatus")
        check("a table removed behind its back shows up at once",
              "'state': <'unprotected'>" in out and "'enforcing': <false>" in out, out)
        check("Disarm works", call("Disarm")[0] == 0 and "'state': <'off'>" in call("GetStatus")[1]
              and our_rules() == [], call("GetStatus")[1] + str(our_rules()))
        rc, out = subprocess.getstatusoutput(
            f"gdbus introspect --session -d {dest} -o {obj} | grep -cE '^ +(Arm|GetStatus|Changed)\\('")
        check("introspection lists the interface", out.strip() == "3", out)
    finally:
        child.kill()


def serve():
    from gi.repository import GLib, Gio
    ksd.BUS_TYPE = Gio.BusType.SESSION
    ksd.log = lambda m: None
    svc = ksd.Service()
    svc.start()
    GLib.MainLoop().run()


GROUPS = {"validate": t_validate, "rulesets": t_rulesets, "failclosed": t_failclosed,
          "inject": t_inject, "mgmt": t_mgmt, "endpoints": t_endpoints, "net6": t_net6,
          "arp": t_arp, "classify": t_classify, "nm": t_nm, "fwd": t_fwd, "churn": t_churn,
          "monitor": t_monitor, "dbus": t_dbus}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    if which == "_serve":
        serve()
    elif which == "_mgmt_reboot":
        t_mgmt("reboot")
    elif which in GROUPS:
        print(f"== {which}")
        GROUPS[which]()
        with open(os.path.join(SCR, "result"), "w") as fh:
            json.dump(RESULTS, fh)
        sys.exit(1 if RESULTS["fail"] else 0)
    else:
        sys.exit(f"usage: lab.py <{'|'.join(GROUPS)}>   (run via tests/run.sh)")
