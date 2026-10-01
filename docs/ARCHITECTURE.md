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
   work before the tunnel it is building exists.
5. the DoH bootstrap servers, tcp/443 to pinned IPs.
6. the chosen resolver.
7. **the DNS backstop.** It must sit *before* the bypass/mgmt/LAN accepts,
   otherwise those holes are also DNS holes. It cannot sit before the exit accept,
   because DNS through the tunnel is the entire point of `dns_mode=tunnel`.
8. bypass, mgmt, DHCP.

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

This is why `strict` is the default rather than `standard`.

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
  it was restored within one tick.

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

`system` on a network that is *not* a trusted node does not mean "blocked". It
means we refuse to steer: NetworkManager's own configuration stands, and because
the nft backstop sits **after** the exit accept, lookups still leave through the
tunnel and still cannot reach the local resolver. That state reports as
`unmanaged`. It only degrades to `blocked` when there is no tunnel either.

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

Certificate hostname verification is disabled for that one request, and only
there: the server is an IP literal, so there is no name to verify against, and
obtaining one would need the DNS this exists to replace. A hostile answer can add
an allowed destination; it cannot read tunnelled traffic.

## State that outlives the process

The daemon has `Restart=on-failure` and survives reboots. Anything it changes on
the running system — resolved overrides, pinned management routes — is therefore
**persisted** under `runtime` in `config.json`, and cleaned up at startup when
disarmed.

This was not the original design and it was wrong. With the record held only in
memory, a single restart orphaned it: disarm left `enp1s0` with `Default Route:
no` and DNS broken system-wide, with nothing left that knew to undo it.

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

A gateway that already tunnels for you can satisfy a profile without a VPN,
matched on IP **and** MAC. MAC binding defeats a hostile network handing out your
router's address; it does not defeat an attacker already on the wire forging a
MAC. Documented tradeoff.

**Trust is opt-in and never inferred** — either name the node in `trusted_node`, or
turn on `auto_node` deliberately. An earlier version matched any configured node
against the current gateway, which meant a setup saying "protect me with VPN X"
would silently drop the VPN whenever the gateway happened to be in the nodes list.
Merely being on a known LAN must not disable the VPN the user asked for.

## Boot

Two units, because "block early" and "run the daemon" want different orderings:

- `kiwi-killswitch-boot.service` — `DefaultDependencies=no`, before
  `network-pre.target`, loads a cached hard block if we were armed.
- `kiwi-killswitchd.service` — before `NetworkManager.service`, replaces it with
  the real ruleset.

Measured on a real reboot:

```
01:40:03.816  Starting kiwi-killswitch-boot.service
01:40:04.087  Finished kiwi-killswitch-boot.service   <- blocked here
01:40:04.450  Started kiwi-killswitchd.service
01:40:04.458  Starting NetworkManager.service         <- 370ms later
```

There is no window.

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
