#!/usr/bin/env python3
"""Headless smoke test for the settings app — run it through tests/run.sh gui.

Builds the real window against a fake daemon, once per state the daemon can
report, and checks the one thing that must never be wrong: what the hero card
says. Then builds every settings page, so a typo in one cannot hide until
someone clicks on it.

Needs a Wayland display; run.sh provides a private headless one (mutter) on a
private session bus, so nothing appears on, or talks to, the real desktop.
"""
import importlib.machinery
import importlib.util
import os
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ld = importlib.machinery.SourceFileLoader(
    "ksgui", os.path.join(HERE, "..", "gui", "kiwi-killswitch-settings"))
spec = importlib.util.spec_from_loader("ksgui", ld)
gui = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
ld.exec_module(gui)

CONFIG = {"vpn": "wg-home", "mode": "full", "trusted_node": "", "auto_node": "0",
          "dns_mode": "tunnel", "dns_custom": "", "dns_dot": "0", "dns_search": "",
          "bypass_subnets": "", "allow_lan": "0", "allow_in_ports": "22",
          "mgmt_subnets": "", "strict_input": "0", "hardening": "strict",
          "hardening_confirmed": "0", "instant_apply": "0", "developer_mode": "1",
          "deadman_sec": "0", "hardening_confirm_sec": "90",
          "doh_servers": "1.1.1.1 9.9.9.9", "doh_path": "/dns-query"}


class FakeDaemon:
    def __init__(self, status):
        self._status = dict({"armed": True, "enforcing": True, "mode": "full",
                             "exit_iface": "", "vpn": "wg-home", "dns_path": "tunnel",
                             "hardening": "strict", "node": "", "conflicts": "",
                             "pending": 0, "detail": ""}, **status)

    def status(self):
        return dict(self._status)

    def config(self):
        return dict(CONFIG)

    def pending(self):
        return {}

    def list_vpns(self):
        return [("wg-home", "wireguard", "yes"), ("work-ovpn", "vpn", "no")]

    def list_nodes(self):
        return [{"name": "m1", "ip": "192.168.50.1", "mac": "", "dns": "192.168.50.1"}]

    def watch(self, cb):
        pass

    def __getattr__(self, name):            # set_pending, apply, arm, ...
        return lambda *a, **k: None


def labels(widget, css):
    out = []
    child = widget.get_first_child()
    while child:
        if isinstance(child, Gtk.Label) and css in child.get_css_classes():
            out.append(child.get_label())
        out += labels(child, css)
        child = child.get_next_sibling()
    return out


CASES = [
    # name, status the daemon reports, hero title, hero tone, pill on the selected VPN
    ("off", {"armed": False, "enforcing": False, "state": "off"},
     "Not protecting", "off", "CONNECTED"),
    ("armed but no ruleset", {"enforcing": False, "state": "unprotected",
                              "detail": "NOT PROTECTED — firewall failed"},
     "NOT PROTECTED", "bad", "CONNECTED"),
    ("blocking", {"state": "blocking", "detail": "blocking (no permitted path)"},
     "Blocking everything", "warn", "CONNECTED"),
    ("tunnel is the exit", {"state": "protected", "exit_iface": "wg0",
                            "detail": "protected via wg0"},
     "Protected", "ok", "PROTECTING"),
    ("a trusted node is the exit", {"state": "trusted-node", "exit_iface": "enp3s0",
                                    "node": "m1", "detail": "trusted node m1"},
     "Protected", "ok", "CONNECTED"),
    ("node mode", {"state": "dns-only", "mode": "node",
                   "detail": "DNS only: lookups forced to 192.168.50.1, internet open"},
     "DNS only — not a kill switch", "warn", "CONNECTED"),
    ("node routed elsewhere", {"state": "blocking", "exit_iface": "enp3s0", "node": "m1",
                               "detail": "trusted node m1 — but the system is routing "
                                         "through another uplink, so traffic is blocked."},
     "Blocking everything", "warn", "CONNECTED"),
    # a daemon from before the `state` field: the app has to work it out itself
    ("old daemon, node mode", {"mode": "node", "detail": "node mode: WAN open"},
     "DNS only — not a kill switch", "warn", "CONNECTED"),
    ("old daemon, protected", {"exit_iface": "wg0", "detail": "protected via wg0"},
     "Protected", "ok", "PROTECTING"),
    ("old daemon, blocking", {"detail": "blocking"}, "Blocking everything", "warn", "CONNECTED"),
]

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += 0 if cond else 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


def run(app):
    print("== gui")
    for name, status, title, tone, pill in CASES:
        win = gui.Window(app, FakeDaemon(status))
        got_title = win.hero_title.get_label()
        tones = [c for c in win.hero.get_css_classes() if c in ("ok", "bad", "warn", "off")]
        pills = labels(win.vpn_group, "ks-pill")
        check(f"{name}: says “{title}” in the {tone} colour",
              got_title == title and tones == [tone], f"{got_title!r} {tones}")
        check(f"{name}: the selected connection is marked {pill}",
              pills[:1] == [pill], str(pills))
        win.destroy()
    win = gui.Window(app, FakeDaemon({"state": "protected", "exit_iface": "wg0"}))
    for builder in (win._page_dns, win._page_mode, win._page_nodes, win._page_reach,
                    win._page_hardening, win._page_advanced):
        try:
            win._reload()
            page = builder()
            check(f"page builds: {page.get_title()}", page is not None)
        except Exception as e:  # noqa: BLE001
            check(f"page builds: {builder.__name__}", False, repr(e))
    win.destroy()
    with open(os.environ.get("GUI_RESULT", "/dev/null"), "w") as fh:
        fh.write(str(fails))
    app.quit()
    return False


app = Adw.Application(application_id="eu.kiwinetwork.KiwiKillSwitch.Test",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
app.connect("activate", lambda a: (a.hold(), GLib.idle_add(run, a)))
app.run(None)
sys.exit(1 if fails else 0)
