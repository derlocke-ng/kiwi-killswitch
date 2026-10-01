# kiwi-killswitch — status log

Newest first. Dates absolute (YYYY-MM-DD). Keep this current in the same change
as the code.

## Done

### 2026-10-01 — the settings app, redesigned

The connection list is now the front page: one checkable row per VPN with a
state pill (PROTECTING / CONNECTED / OFF), under a hero card that states the
answer in one word and one colour (green protected, red NOT PROTECTED, amber
blocking, grey off). Conflicting tunnels and provisional paranoid hardening
moved to an `Adw.Banner`. Nav rows gained icons. A small stylesheet keyed to
libadwaita's named colours, so it follows the user's theme and accent.

Single selection is enforced by hand rather than with a radio group, because a
radio group cannot express "none" — a real choice when a trusted node is
standing in for the VPN.

Verified by rendering the window and looking at it (`shoot_x11.py`), not by
reading the code.

**Operational note that cost a round:** `sudo ./install.sh install` updates only
the daemon. The settings app and extension are the *user* scope. Updating one
and not the other leaves a new daemon driving an old UI.

### 2026-10-01 — dropped the profile layer

The first cut modelled every setting as belonging to a named profile. That was
the wrong shape and the user said so plainly: the point of the application is
picking **which VPN connection is protected**, and profiles put bookkeeping in
front of it — to the point where DNS could not be set at all until a profile
existed.

Now there is one live configuration and one flat settings namespace. The VPN
connection list sits on the settings app's front page with a radio button per
connection, and in the Quick Settings menu. `SetPending`/`Apply` staging is
unchanged — that part was asked for and works. Removed: `ListProfiles`,
`GetProfile`, `WriteProfile`, `DeleteProfile`, `active_profile`, `auto_profile`,
per-node `profile` field. Added: `trusted_node` and `auto_node` as plain
settings, and `GetConfig` now reports only what is *in effect* (staged values
come from `GetPending`), because a `config` listing that showed unapplied values
as live would recreate the confusion staging exists to prevent.

Re-verified on the test box after the refactor: fail-closed with the VPN down,
auto-sync on VPN up, the one-VPN rule (`riseup-ovpn` still could not establish
and was named in `conflicts`), staging, and a full disarm restore.

### 2026-10-01 — v0.1.0, built and verified on hardware

Daemon (`daemon/kiwi-killswitchd`, ~1000 lines): `Config` (staged + active,
persisted runtime ownership), `Resolver` (DoH, stdlib DNS wire codec), `Net`,
`Eval`/`Profile`, `Firewall` (inet output/input/forward + netdev egress), `Dns`,
`KillSwitch`, `NMMonitor`, `LinkMonitor`, reconcile, `Service` (D-Bus).
Plus the gdbus CLI, GTK4 settings app, GNOME Quick Settings extension, bus
policy, two systemd units, dual-scope installer.

Verified on **Bluefin 44.20260929 at the test machine** against real WireGuard
(`id-bluefin-dev`) and real OpenVPN (`riseup-ovpn`), with firewalld active:

- fail-closed with no VPN (HTTPS, HTTP, ICMP, raw TCP, DNS all blocked; admin
  path alive)
- auto-sync on VPN up, exit IP `the machine's real address` → `the VPN's exit address`, confirmed by
  `wg transfer` counters and `ip route get`
- tunnel killed at link level → no leak, and status names the NM/kernel
  disagreement
- one-VPN rule: `riseup-ovpn` could not establish while armed for WireGuard; no
  `tun0` ever appeared; reported as `conflicts`
- DNS: queries resolved `-- link: id-bluefin-dev`; a packet at the LAN resolver
  hit the backstop
- second uplink (macvlan, stealing the default route at metric 10) could not leak
- forward chain: container reached the internet at `standard` with the host
  blocked; blocked at `strict`
- netdev egress: raw `AF_PACKET` frame left the machine at `strict`, was dropped
  at `paranoid`, with the inet counter at 0 in both cases
- paranoid watchdog auto-reverts when not confirmed
- trusted node satisfied a profile; a wrong MAC broke trust immediately
- external `nft delete table` → `NOT PROTECTED` instantly, re-asserted next tick
- `firewall-cmd --reload` did not remove our tables
- reboot: boot unit finished 370 ms before NetworkManager started
- disarm restored nftables, DNS and routes completely

Local harnesses (no root, no network changes): 31 ruleset variants generated and
loaded under `unshare -rn`; config/profile/DNS-selection logic; the full D-Bus
surface driven over the session bus.

### 2026-10-01 — two more found on a two-uplink machine

8. **A trusted node on a second uplink was invisible.** `_match_node()` asked
   `Net.default_gw()`, which returns only the *lowest-metric* default route.
   With two NICs up — coming back onto the node's LAN while a tether or a NAT
   uplink is still connected — the node's route is frequently not the winner,
   so the node was never recognised, the switch fell back to demanding the VPN,
   and the other uplink's WAN carried traffic. Reported from the field, then
   reproduced on the test box with `enp7s0` (libvirt NAT) alongside `enp1s0`
   (gateway `the node's gateway address`, the real node). Now every default route is checked,
   routes through a tunnel are skipped, and whether the kernel actually prefers
   that uplink is reported separately as `node_not_preferred`.
9. **Route preference was judged by metric.** Metrics are not the authority:
   policy rules outrank them (this box has a Tailscale rule at priority 5270),
   and NetworkManager rewrites metrics on reactivation — observed going from
   `20106` to `106` minutes apart on the same link. `Net.route_dev()` now asks
   the kernel's FIB with `ip route get` against a TEST-NET address, which sends
   no packets.
10. **`ip route del <prefix>` deleted the wrong route — and cut the admin LAN.**
    `_del_mgmt_routes()` issued a bare prefix delete. When our pin is already
    gone (NM reapplied, link bounced, machine rebooted) that matches the
    kernel's own on-link route instead, so the startup cleanup removed
    `192.168.0.0/16` and the test machine dropped off its LAN mid-install.
    Pins are now recorded with gateway, device and an explicit metric, and
    deleted exactly; a record from an older version is dropped without touching
    the routing table, because there is no safe way to delete it.

11. **A libvirt/podman bridge is not a tunnel, but `is_tunnel()` says it is.**
    `is_tunnel()` works by "no backing device in sysfs", which is right for
    spotting a tunnel and also true of every bridge. `routed_via_tunnel()`
    therefore answered *yes* for a NAT'd VM's own subnet, so putting that subnet
    in `mgmt_subnets` — the documented way to let a VM reach the host's DHCP and
    DNS — would have pinned the VM network out the physical NIC and severed the
    host from its guests. `Net.is_onlink()` now refuses to pin any attached
    network, which protects the admin LAN for the same reason.

12. **A full tunnel swallowed the management subnet, and the session died.**
    A VPN installs a policy rule sending everything into its own table, so a
    connected route in `main` is never consulted and an on-link admin LAN ends
    up inside the tunnel. The old code tried to fix this by pinning the subnet
    `via <gateway>`, which is wrong for an attached network — and once the
    on-link guard (11) correctly stopped doing that, nothing kept the LAN out of
    the tunnel at all, so bringing a VPN up cut SSH. Every management subnet now
    gets `ip rule add to <subnet> lookup main priority 90`, which restores the
    connected route without touching any route; the `via <gateway>` pin is kept
    only for subnets that are genuinely off-link. Measured in a namespace:
    without the rule `ip route get the admin workstation` answers `dev wg0 table
    52241`; with it, `dev lan0`, while `1.1.1.1` still goes to the tunnel.

14. **A trusted node and the selected VPN could both be up, and the node won.**
    The node matched first and became the exit, while the connected VPN quietly
    owned the default route — so every packet left on an interface the ruleset
    did not permit and everything was blocked. Connecting your VPN made things
    strictly worse. The exit is now whichever path the kernel is *actually*
    routing through, with the tunnel preferred when neither matches. Verified on
    the test box: `trusted node m1` before the VPN comes up, `protected via
    id-bluefin-dev` after, with the admin LAN staying on `enp1s0` throughout.

13. **`dns_path` reported "blocked" when DNS was working.** With
    `dns_mode=system` on a network that is not a trusted node, the daemon
    deliberately stops steering resolved and leaves NetworkManager's
    configuration standing. With a full tunnel up that points resolved into the
    tunnel, and the nft backstop sits *after* the exit accept — so lookups
    resolve, and still cannot reach the local network's resolver. Reported as
    `fallback` now — it uses the resolver the tunnel provides and says the
    network is not trusted. Leaving it unsteered also worked, but resolved then
    kept retrying local resolvers the backstop was dropping, costing a timeout
    per lookup. `unmanaged` remains for the case where the tunnel pushes no DNS
    at all.

### Measured: what each subnet setting actually does

Asked in the field, so it was answered with rules rather than prose. Same
subnet, a NAT'd guest built with veth + netns + masquerade (the shape libvirt
gives a VM):

| setting | rules | guest → a host service |
|---|---|---|
| `bypass_subnets` | `ip daddr … accept` in output and forward | blocked |
| `mgmt_subnets` | the above plus `ip saddr … accept` in **input** | HTTP 200 |

Guest → internet returned the VPN exit address in both cases, i.e. forwarded
out the tunnel and still covered. `allow_lan` is `bypass_subnets` with the
on-link subnets filled in automatically — same mechanism, same direction.

### `auto_node` now overrides a specific pick

With a node named in `trusted_node`, turning on `auto_node` previously did
nothing — the name filter still applied. A switch labelled "accept any trusted
node" that silently does nothing is worse than either behaviour, so it now
means what it says and the settings app greys out the picker while it is on.

### Bugs found by testing and fixed — do not regress

1. **Bus policy denied root.** Only group `wheel` could call the daemon, so
   `install.sh`'s readiness check and — much worse — `install.sh uninstall`'s
   `disarm` failed *silently*, and root had no TTY recovery. Root now has send
   access; it grants nothing new, since root can already rewrite the config.
2. **Object registered after the name was claimed.** The bus grants the name
   immediately, so a client watching for it could call in the gap and get
   "object does not exist at path". Register the object first, then request the
   name.
3. **`iface_up()` never returned False.** `ip link show <dev> up` exits 0 with no
   output for a device that is down. A downed tunnel keeps its address, so it
   still resolved as the exit and status reported "protected" while nothing was.
   Parse the interface flags instead.
4. **Node trust was inferred, not opted into.** Any profile was satisfied by any
   configured node that happened to be the gateway — so being on a known LAN
   silently disabled the VPN the user had selected. Trust now requires the
   profile to name the node.
5. **The paranoid watchdog never fired.** It was re-armed on every apply, and
   apply runs on every link event and reconcile tick, so the deadline was
   perpetually postponed. Set once, and left alone while running.
6. **Runtime-owned state was in memory only.** The daemon restarts on failure and
   across reboots; a restart orphaned the resolved overrides and pinned routes,
   so disarm left `enp1s0` with `Default Route: no` and DNS broken system-wide.
   Now persisted under `runtime` in the config and cleaned up at startup.
7. **`resolvectl revert` is not a clean undo.** It drops NetworkManager's servers
   too and NM never re-pushes them, not even on `nmcli device reapply`. `clear()`
   now restores NM's own answer, read from `nmcli device show`.
8. EAPOL (`ether type 0x888e`) added to the netdev chain — `wpa_supplicant` sends
   it via `AF_PACKET`, so WPA-Enterprise could not associate at all.
9. CLI dict formatting produced unbalanced quotes; `nodes` was unformatted.
10. Status kept stale `mode`/`hardening`/`profile` after disarm.

## Next

- [ ] Confirm the repo/org name and D-Bus name before first publish. Currently
      assumed: `github.com/derlocke-ng/kiwi-killswitch`,
      `eu.kiwinetwork.KillSwitch`, extension `kiwi-killswitch@kiwi-network.eu`.
- [ ] Click through the GTK app and the GNOME extension in a real desktop
      session. Every page is built and walked automatically against a live
      daemon (`gui_render_test.py`), and the extension loads under gjs with
      stubbed shell modules, but neither has been driven by hand.
- [ ] Test with a genuine second physical NIC (the macvlan test is a good proxy,
      not the real thing) — ideally wifi + ethernet, or a USB tether.
- [ ] Test a kiwi-node that actually is the default gateway and does tunnel
      (the node's gateway address / .251), rather than the LAN router standing in for one.
- [ ] Exercise the DoH resolver against a VPN whose endpoint is a hostname. Both
      test connections use literal IPs, so that path is unit-tested but has not
      run against a real hostname endpoint while armed.
- [ ] IPv6: the rulesets are `inet` and cover it, but the test LAN is v4-only, so
      the v6 paths are untested in practice.
- [ ] `kiwi-updater` + `kiwi-catalog` (phase 2), then list this app twice
      (user + `scope=system`).

## Known limits

- Paranoid mode ties the netdev chain to NICs that exist at apply time. A NIC
  appearing later is covered by the inet layer until `LinkMonitor` rebuilds.
- Trusted-node MAC binding does not stop an on-link attacker forging a MAC.
- DoH certificate hostname verification is disabled for endpoint lookup, because
  the server is an IP literal. Scoped to that one request.
- Node mode (`mode=node`) is not leak protection and the UI says so.
- Two enforcers fight. Do not run this alongside `nova-killswitch` / `nova-vpn`.
