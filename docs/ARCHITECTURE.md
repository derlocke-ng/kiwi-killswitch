# kiwi-killswitch — architecture

Why the thing is shaped like this, and which parts are load-bearing. Read this
before changing the firewall, DNS or routing paths: several rules below look
redundant and are not, and each one has a leak or a lockout behind it.

## The problem

A VPN client on the host leaks by default. The tunnel and the traffic it protects
share one network namespace, so when the tunnel dies the kernel simply reroutes
onto the physical NIC and everything continues with the real source address.
Nothing announces this. Applications do not notice, NetworkManager still reports
the connection as *activated*, and the desktop VPN indicator stays lit.

So the kill switch cannot be "turn traffic off when the VPN drops". It has to be
"nothing may leave except on a path I have verified, and the absence of such a
path is a block, not a fallback".

## Three processes, one that matters

```
┌─ user session (unprivileged) ───────────────┐  ┌─ root ─────────────────────┐
│ GNOME Quick Settings toggle  (extension.js) │  │ kiwi-killswitchd           │
│ kiwi-killswitch-settings     (GTK4)         │─▶│  nftables: inet + netdev   │
│ kiwi-killswitch              (CLI, gdbus)   │  │  systemd-resolved steering │
└─────────────────────────────────────────────┘  │  NM + kernel watchers      │
          D-Bus system bus, bus policy: `wheel`   └────────────────────────────┘
```

A GNOME extension is an unprivileged process in the compositor. It cannot touch
nftables, and it must not be able to. Enforcement therefore lives entirely in the
daemon, and the three front-ends are interchangeable views over one D-Bus
interface.

**Authorization is the D-Bus bus policy, not pkexec and not sudoers.** Group
`wheel` may call the daemon's methods; everyone else is denied by the system
bus's default-deny. This is what makes arming passwordless, and it is safer than
the alternatives because the surface it grants is a fixed set of validated
methods rather than "execute this program as root".

**That makes every value untrusted input.** Any process in a `wheel` session can
call these methods, so each value is validated when it is staged
(`clean_setting`, `clean_node`) and refused with a reason if it cannot be used.
Every token is then checked a *second* time on its way into a ruleset, because
the config on disk can predate the checks. Both layers are there because both
failures happened. A node's resolver field, stored as typed, was a way to write
arbitrary nft statements as root — and, through `include` and nft echoing the
offending line into the journal, to read root-only files. And a port list
containing `99999` was rejected by nft *together with the fallback that embedded
the same value*, which left an armed machine with no table at all.

Root is granted send access too. That is not a privilege increase — root can
already rewrite the config and restart the service — but without it `install.sh
uninstall` cannot disarm before removing the daemon, and root cannot recover from
a TTY. Both failed silently before this was added.

## The firewall

Two tables, built together and swapped in **one** `nft -f` transaction using the
`add table` + `delete table` + re-add idiom (`add table` is a no-op when the table
exists, unlike `add chain`, which is what makes the unconditional `delete` safe).
There is never a moment where the old rules are gone and the new ones are not yet
in place.

### `table inet kiwi_ks`, priority −10

`output`, `input` and (at `strict`/`paranoid`) `forward`, all `policy drop`, v4
and v6 together.

**Priority −10 is chosen against firewalld**, which puts its own filter chains at
10 (`NFT_HOOK_OFFSET` in `firewall/core/nftables.py`). We therefore run first. A
`drop` is terminal across base chains while an `accept` only ends the current
chain, so the kill switch blocks what it must *and* firewalld still filters
everything we pass. Verified on Bluefin 44 with firewalld 2.4.4 active: both
apply, and `firewall-cmd --reload` does not remove our tables — its
`build_flush_rules()` deletes only `table inet firewalld`.

Rule order in `output` is not cosmetic:

1. `oifname "lo"` — applications talk to the resolved stub on 127.0.0.53 through
   this, always.
2. the pinned established rule (below).
3. the exit interface.
4. the selected VPN's endpoints — over *any* uplink, because the handshake has to
   work before the tunnel it is building exists — and **only the protocol and
   port the tunnel itself uses**: WireGuard's peer port over UDP; for OpenVPN
   each `remote` with its own port and the connection's protocol; through a
   proxy, the proxy. An address-only rule let *any* traffic to that host leave
   around a dead tunnel, and a self-hosted VPN server usually serves other
   things too. An endpoint whose port cannot be determined still gets the
   address-only rule: a tunnel that cannot connect is the failure that makes
   people disarm.
5. the DoH bootstrap servers, tcp/443 — **only while a hostname endpoint needs
   resolving and nothing carries traffic, and only for root's sockets**
   (`meta skuid 0`). Emitted unconditionally it let every process reach those
   resolvers from the real address while the switch said "blocking everything".
6. the chosen resolver — **only when it is not reached through the tunnel.**
   Through the tunnel the exit accept already covers it, and an address rule
   with no interface on it would pass lookups onto the physical NIC in the
   moment between a tunnel dying and the ruleset being rebuilt.
7. **the DNS backstop.** It must sit *before* the bypass/mgmt/LAN accepts,
   otherwise those holes are also DNS holes. It cannot sit before the exit accept,
   because DNS through the tunnel is the entire point of `dns_mode=tunnel`.
8. bypass, mgmt.
9. IPv6 neighbour discovery (solicit, advert, router-solicit). IPv6 has no ARP:
   the kernel resolves a next hop with ICMPv6, and unlike ARP those packets *do*
   pass this hook. Dropped, no IPv6 neighbour is ever resolved, so a VPN server
   reached over IPv6 could never be handshaken with. Link scope only.
10. DHCP.

### `table netdev kiwi_ks_egress`, one chain per physical NIC

The `inet output` hook **cannot see `AF_PACKET` egress**. NetworkManager's own
DHCP client and anything holding `CAP_NET_RAW` write frames straight to the
device, bypassing the IP stack entirely. The netdev egress hook sits on the
device and sees them.

Measured on the test box, armed, with a hand-built Ethernet+IP+UDP frame written
via `AF_PACKET`:

| hardening | result | `ks-drop-out` | `ks-drop-egress` |
|---|---|---|---|
| `strict` | **frame left the machine** | 0 | (no chain) |
| `paranoid` | `ENOBUFS`, dropped at the device | 0 | 1 |

The inet counter stays at 0 in both cases — it never saw the packet. That is the
whole argument for this layer.

Tunnelled traffic reaches this hook already **encapsulated** and addressed to the
VPN endpoint, so `ip daddr <endpoint>` covers it, and anything else leaving a
physical NIC unencapsulated is by definition a leak.

Consequences that are easy to get wrong:

- **No conntrack in the netdev family.** Matching is address/port only. That is
  sufficient, because the question here is "may this address be spoken to at
  all", not "is this a reply".
- **A chain naming a device the kernel does not have fails the whole
  transaction.** Chains are generated only for NICs that exist right now;
  `LinkMonitor` rebuilds on every add/remove, and until it does the `inet output`
  chain still covers the newcomer.
- **Virtual uplinks are covered through their parent.** A macvlan/VLAN/bridge
  child has no sysfs `device` link and so gets no chain of its own, but its frames
  egress via the parent and hit the parent's chain. Verified: a raw frame written
  to a macvlan child was caught by the parent's `egress_enp1s0` chain.
- **EAPOL must be allowed** (`ether type 0x888e`). `wpa_supplicant` writes it via
  `AF_PACKET`, so dropping it means WPA-Enterprise wifi cannot associate at all —
  and it carries no IP payload, so it cannot leak an address.
- **`paranoid` is provisional on first apply.** One wrong allow-rule takes the
  machine fully offline with no way back in. It reverts itself to `strict` unless
  confirmed, and escalating *into* paranoid always re-arms that deadline even for
  a previously-confirmed setup, because the allow-list is built from settings
  that may have changed since.

### The `forward` chain

`ip_forward` is commonly 1 on a developer workstation (podman, libvirt, Waydroid).
Forwarded packets **never traverse the output hook**, so without this chain a
container is a hole straight through the kill switch. Measured, with the host
itself fully blocked because the tunnel was dead:

| hardening | container reaching the internet |
|---|---|
| `standard` | **yes — while the host was blocked** |
| `strict` | no (`ks-drop-fwd` counted the drops) |

With the tunnel *up*, `strict` forwards the container out through the tunnel, so
it shares the host's exit IP. That is intended: containers are protected, not
severed.

An allowed network (`bypass_subnets`, `allow_lan`) is allowed for guests in both
directions of a conversation the guest started: `ip daddr <net> accept` going
out, and `ip saddr <net> ct state established,related accept` coming back.
Without the second rule a guest could send to the LAN and never hear the answer.
It is limited to established flows from that network, so the LAN still cannot
open a connection *into* a guest.

This is why `strict` is the default rather than `standard`.

## Which interface is what

Three classes, and the difference is load-bearing:

| class | how it is recognised | used for |
|---|---|---|
| **tunnel** | *positively*: WireGuard's `DEVTYPE`, a tun/tap's `tun_flags`, or a tunnel ARPHRD type — and not hardware, not on hardware, not a bridge, bond or bridge port | the only thing an exit may be |
| **uplink** | a real NIC, or something stacked on one (bridge, bond, VLAN — sysfs `lower_*`) | trusted nodes, the LAN, management routing |
| neither | everything else: `virbr0`, `docker0`, veth, dummy | nothing |

There used to be one test — "no backing device in sysfs" — answering both
questions. It is true of every tunnel, and of every bridge, bond and VLAN. So on
a machine whose uplink is `br0` the "an exit is never a physical NIC" gate let
the LAN interface through, and the same test hid that uplink from the node, LAN
and management logic. The first half is a leak: NetworkManager reports the
*base* device for a plugin VPN that has no address yet (seen live:
`ignoring non-tunnel iface 'enp1s0' for 'riseup-ovpn'` while OpenVPN was still
connecting), and on a bridged host that base device would have become the exit.

## One VPN, and why it is enforced only by the firewall

Several simultaneous tunnels fight over the routing table and over policy-routing
priorities. The predecessor project grew an elaborate `ip rule` construction
(priorities 5280/5281 and a private table) purely to make a tunnel stacked on
NetworkManager's WireGuard carry traffic. Permitting exactly one connection
deletes that entire mechanism, and with it the most fragile code in the design.

Enforcement is **firewall-only**: only the selected connection's endpoints are
reachable, so any other tunnel simply cannot complete a handshake. The daemon
never calls `nmcli connection up/down`. A daemon that downs connections races
NetworkManager's autoconnect forever, and it takes away a decision that belongs
to the user.

Verified: armed for a WireGuard connection, starting `riseup-ovpn` left
NetworkManager reporting it active while **no `tun0` was ever created** — its
handshake to `204.13.164.252` was dropped. The exit never moved, and status
reported `conflicts = riseup-ovpn`.

## Knowing what is actually alive

Three independent sources, because each one misses something:

- **`NMMonitor`** — NetworkManager D-Bus signals.
- **`LinkMonitor`** — `ip -o monitor link addr route`, restarted if it ever dies.
  This is not redundant with the above. **A tunnel that dies at the link level
  emits no NetworkManager signal**: NM still reports the connection as activated,
  and GNOME still draws a connected VPN. Verified on the test box —
  `nmcli` said `id-bluefin-dev:activated` with the interface `DOWN`. Without this
  watcher the ruleset keeps pointing at a dead interface and the only way back
  online is to disarm, which is exactly when the real address is exposed. **Do not
  remove it.**
- **the reconcile tick** — re-asserts a vanished ruleset and catches drift both
  watchers missed. Verified: deleting the table externally flipped status to
  `NOT PROTECTED` instantly (it is recomputed from the kernel on every read) and
  it was restored within one tick. Every five minutes it re-evaluates
  regardless, for what no snapshot shows: an endpoint edited in NetworkManager,
  a gateway whose MAC changed, a resolver setting NM pushed back.

### Idle has to be idle

The tick compares a snapshot of links, addresses and routes. `ip addr` prints
the seconds a DHCP lease has left, so the raw text differed on *every* reading:
on any DHCP machine the daemon saw "drift" each tick and re-applied everything,
four times a minute, for as long as it was armed. That reset the `ks-drop-*`
counters — the evidence of what was blocked — flushed the resolver cache, and
signalled both front-ends, one of which answered with synchronous D-Bus calls
from inside the compositor. On the test box 145 of 392 logged applies were this.

So the snapshot leaves lifetimes out, and an apply that changes nothing does
nothing: an identical ruleset is not reloaded (once the kernel confirms the
table is there), the resolver cache is flushed only when the DNS path changes,
`Changed` is emitted only when the status did, and the journal gets a line per
change of outcome rather than per apply. Measured after: 50 seconds armed and
idle, zero applies, zero signals, drop counters intact.

Both watchers share one debounce timer, with a ceiling — a link that flaps
without pause cannot postpone the sync for ever.

### `iface_up()` is not what it looks like

`ip link show <dev> up` exits **0 with no output** when the device exists but is
down. Reading its return code as liveness is silent and costly: a downed
WireGuard interface keeps its address, so it still resolves as the tunnel, and the
daemon reports `protected via id-bluefin-dev` while nothing is protected. We parse
the interface flags instead and require `UP` without `NO-CARRIER`.

When NetworkManager and the kernel disagree, status says so in words rather than
asking "is the VPN up?" — because the user is looking at a GNOME panel that says
it is.

## DNS

systemd-resolved runs in stub mode, so applications always query `127.0.0.53`,
which `oifname "lo"` permits unconditionally. **The leak surface is resolved's own
upstream query**, and the only thing that decides where that goes is resolved's
per-link configuration. So we set it directly with `resolvectl`, and back it with
an nftables rule that drops `udp/53` and `tcp/{53,853}` toward anything but the
chosen resolver.

| mode | resolver |
|---|---|
| `tunnel` | what the VPN pushed |
| `system` | the network's own — **only** on a gateway that matches a trusted kiwi-node |
| `custom` | a fixed address, optionally over DoT |

`system` on a network that is *not* a trusted node does not mean "blocked". The
network's own resolver is not offered, so the tunnel's is used instead and the
state reports as `fallback` — or `unmanaged` when the tunnel pushes no resolver
at all, where NetworkManager's configuration stands and the backstop, sitting
**after** the exit accept, still keeps lookups inside the tunnel. It only
degrades to `blocked` when there is no tunnel either.

`custom` is the one mode whose resolver is reachable **outside** the tunnel
while the tunnel is down. That is deliberate — it is the only way a VPN with a
hostname endpoint can find its server while armed — and it means lookups go to
that resolver from the real address in that state. Use DoT with it.

Every other link gets `default-route no`, which is what keeps the LAN's resolvers
out of the picture **without rewriting the NetworkManager profile**. We
deliberately do not touch `nmcli connection modify … ipv4.ignore-auto-dns`: that
edits the saved profile and needs a reactivation to take effect, i.e. the daemon
would have to bounce the user's connection.

Verified armed in `tunnel` mode: `resolvectl query` resolved `-- link:
id-bluefin-dev`, and a hand-built UDP packet to the LAN resolver hit the backstop.

### `resolvectl revert` is not a clean undo

It drops **NetworkManager's** servers along with ours, and NM never notices — not
on `nmcli device reapply`, not on its own. The link is left with no resolver at
all and DNS breaks system-wide, long after the kill switch was disarmed.
Reproduced on the test box. So `Dns.clear()` reverts *and then puts NM's own
answer back*, read from `nmcli device show`. Restoring the servers is also what
makes resolved recompute `DefaultRoute` as yes again.

### Endpoint lookup while armed

A VPN whose server is a hostname must be resolved *before* the tunnel exists,
when normal DNS is blocked. And `nft` resolves hostnames itself at load time, on
the host — so a hostname in the ruleset means a failed load, and because `nft -f`
is one transaction that takes the **whole kill switch** down at the worst moment.

So no hostname ever reaches a ruleset. The daemon resolves them itself over DoH,
to a **literal IP** the ruleset explicitly permits — no bootstrap loop, nothing to
resolve first. Every answer is pinned (all of them, because round-robin means the
address we allowed and the one the client dials can differ) and cached in
`config.json`, so a boot-time restore has addresses before the network is usable.

The certificate **is** verified, against the address that was dialled. The big
public resolvers (1.1.1.1, 9.9.9.9, 8.8.8.8) all carry their IPs as
subjectAltName entries, so nothing has to be resolved first; an earlier revision
switched verification off on the assumption that it was impossible. It matters:
an unverified answer lets whoever runs the local network choose which addresses
the ruleset whitelists. A server without an IP SAN is configured as
`ip#hostname` and checked against the name. One hostname is asked about at most
every two minutes — every lookup is a blocking call on the main loop.

**What this does not do** is resolve the name for the VPN client. NetworkManager
and OpenVPN look their endpoint up themselves, through the system resolver, and
that is blocked while no tunnel is up. So today a hostname endpoint can
reconnect while armed only with `dns_mode=custom`. See "Known limits" in
`docs/TODO.md`.

## State that outlives the process

The daemon has `Restart=always` and survives reboots. Anything it changes on
the running system — resolved overrides, pinned management routes — is therefore
**persisted** under `runtime` in `config.json`, and cleaned up at startup when
disarmed.

This was not the original design and it was wrong. With the record held only in
memory, a single restart orphaned it: disarm left `enp1s0` with `Default Route:
no` and DNS broken system-wide, with nothing left that knew to undo it.

The record says what we **own**, so it can be undone exactly. It does not say
what **exists** — only the kernel does. Management routing once asked the record
both questions, so a policy rule that had vanished was never put back, and a
reboot while armed is enough for that: rules do not persist, the VPN took the
default route, and the admin LAN went into the tunnel with it. Every apply now
checks the kernel and repairs what is missing.

## Failing closed, in layers

"A ruleset that fails to apply must block" is easy to state and was violated
four different ways. What is in place now:

- **`apply()` never raises.** Anything that goes wrong before a ruleset is
  loaded ends in a block and a status that says so. It used to be able to raise
  — a malformed hostname in a connection was enough — and then `armed` was
  already saved, no table was loaded, status still read "disarmed", and the next
  start crashed on the same input.
- **Three rulesets, each needing less than the one before:** the real one; the
  hard block (management path only); the bare lockdown, built from no setting
  at all, so no setting can break it.
- **Steps that sit on top of the firewall cannot take it down.** DNS steering
  and management routing run after the ruleset is loaded; if one fails, that is
  logged and the ruleset stays.
- **An unreadable config is not "defaults".** A missing file is a fresh install.
  A file that exists and does not parse falls back to the last good copy
  (`config.json.bak`), and failing that the machine is treated as armed with
  nothing permitted. Saves are fsynced, because a rename without one can
  survive a power cut as an empty file.

## One flat configuration, and the Apply transaction

There is **one live configuration**, not a set of named profiles. An earlier
revision modelled every setting as belonging to a profile, and it was wrong in a
way worth recording: the thing people actually come to this application for is
*which VPN connection is protected*, and wrapping that in profile bookkeeping put
a layer of bookkeeping in front of the one control that matters — to the point where DNS
could not be configured at all without first creating a profile. The connection
list now sits on the front page and every setting is a plain key.

Edits are staged in `pending` and promoted by `Apply()` in one step, after which
the new ruleset is installed by a single `nft` transaction. There is no
half-applied state and **no disarm in between**, which is what makes reconfiguring
*while already on the untrusted network* safe. `instant_apply` turns staging off
for people who prefer it.

`GetConfig` deliberately reports only what is **in effect**; staged values come
from `GetPending`. A `config` listing that showed unapplied values as if they were
live would recreate exactly the confusion staging exists to prevent.

## Trusted kiwi-nodes

A gateway that already tunnels for you can stand in for the VPN,
matched on IP **and** MAC. MAC binding defeats a hostile network handing out your
router's address; it does not defeat an attacker already on the wire forging a
MAC. Documented tradeoff. A node saved without a MAC is matched on its IP alone,
and any network that hands out that gateway address then opens the switch.

The MAC is read with **one raw ARP request**, not by pinging the gateway and
looking in the kernel's neighbour table. The ping is an ordinary packet, so the
kill switch's own drop policy stops it before the kernel has any reason to ARP —
which left a MAC-bound node unrecognised whenever the machine came up armed.
ARP is not IP and never passes the inet hooks.

**Trust is opt-in and never inferred** — either name the node in `trusted_node`, or
turn on `auto_node` deliberately. An earlier version matched any configured node
against the current gateway, which meant a setup saying "protect me with VPN X"
would silently drop the VPN whenever the gateway happened to be in the nodes list.
Merely being on a known LAN must not disable the VPN the user asked for.

## Boot

Two units, because "block early" and "run the daemon" want different orderings:

- `kiwi-killswitch-boot.service` — `DefaultDependencies=no`, before
  `network-pre.target`. If we were armed it builds a hard block from the config
  and loads it; if that is rejected, the bare lockdown.
- `kiwi-killswitchd.service` — before `NetworkManager.service`, replaces it with
  the real ruleset.

Both are ordered *after* `nftables.service` and **neither pulls it in.** Fedora's
unit runs `nft flush ruleset` when it stops. An earlier `Wants=` started it on
every machine that had left it disabled, and from then on each shutdown ended
with our tables — and firewalld's — being flushed while the links were still
up. We need the `nft` binary, which needs no unit.

Measured on a real reboot:

```
01:40:03.816  Starting kiwi-killswitch-boot.service
01:40:04.087  Finished kiwi-killswitch-boot.service   <- blocked here
01:40:04.450  Started kiwi-killswitchd.service
01:40:04.458  Starting NetworkManager.service         <- 370ms later
```

There is no window. Re-measured after the changes above, rebooting armed with a
MAC-bound node and a management subnet: block loaded at `:43.044`,
NetworkManager starting at `:43.184`, the management rule restored and the node
recognised by `:44.323`, the SSH session back without a gap in the rules.

## Tests

`tests/run.sh` runs the real daemon file inside throwaway user + network
namespaces — real nft, real routing, real sockets, no root, nothing touched on
the machine. Every check is named for a failure that actually happened. It does
not replace `docs/RUNBOOK.md`: NetworkManager, resolved and hardware are not in
it.

## Deliberately not done

- **No connection management.** Ever. See above.
- **No DNS interception or redirection**, no dnsmasq in the path, no per-app
  routing, no namespaces.
- **Nothing in `/usr`** — it is ostree-immutable. Root parts live in `/usr/local`
  (a symlink to the writable `/var/usrlocal`), config in `/etc/kiwi-killswitch`.
- **No third-party Python.** PyGObject plus the standard library, and
  `nft`/`ip`/`nmcli`/`wg`/`resolvectl`, all of which are in the base image.
- **Not Tails or Whonix.** This stops network-level leaks. It does nothing about
  browser fingerprinting, WebRTC, or an application that phones home *through* the
  tunnel.
