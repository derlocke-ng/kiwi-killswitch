# kiwi-killswitch

A **fail-closed VPN kill switch** for GNOME on Fedora Silverblue / Bluefin, built for
the [Kiwi Network](https://kiwi-network.eu). One root daemon owns the firewall; a GNOME
Quick Settings toggle, a GTK4 app and a CLI drive it over the D-Bus system bus — with no
password prompt, ever.

It protects **one** VPN connection at a time, WireGuard or OpenVPN. It does not connect
your VPN for you: you bring connections up in GNOME Settings, and the kill switch builds
a leak-proof firewall around whatever is actually alive.

```
┌─ your session (unprivileged) ─────────────┐   ┌─ root ──────────────────────┐
│  GNOME Quick Settings toggle              │   │  kiwi-killswitchd           │
│  kiwi-killswitch-settings (GTK4)          │──▶│   nftables  (inet + netdev) │
│  kiwi-killswitch (CLI)                    │   │   systemd-resolved steering │
└───────────────────────────────────────────┘   │   NM + kernel event watcher │
             D-Bus system bus, group `wheel`    └─────────────────────────────┘
```

A GNOME extension is an unprivileged process: it *cannot* and *must not* touch the
firewall. So enforcement lives in the daemon, and the UI only asks.

## What it does

- **Fail-closed.** `policy drop` on output, input and forward, IPv4 and IPv6, swapped
  atomically. Traffic leaves only through the protected tunnel. If the VPN drops, you
  reboot, or you switch Wi-Fi → tethering, everything stays blocked until a permitted
  path is back.
- **New interfaces cannot leak.** Plug in USB tethering or turn on Wi-Fi while armed and
  it is blocked by default, not allowed by omission.
- **Three enforcement depths.** *Standard* filters output and input. *Strict* (default)
  also filters **forwarded** traffic — containers, VMs and Waydroid route through the
  host, and forwarded packets never pass the output hook. *Paranoid* adds a **netdev
  egress** chain on each NIC, the only layer that sees raw-socket traffic.
- **DNS cannot wander off.** Choose: through the VPN, the network's own
  (trusted kiwi-nodes only), or a resolver you pick — applied through
  systemd-resolved's per-link settings, with an nftables backstop that drops every other
  lookup.
- **Endpoint lookup that works while armed.** A VPN whose server is a hostname has to be
  resolved before the tunnel exists, when normal DNS is blocked. The daemon queries a
  configured **DNS-over-HTTPS** server directly, at a pinned IP the ruleset permits, and
  caches every answer.
- **Pick a connection, then Apply.** The front page lists your VPN connections; tick
  the one to protect. Changes are staged, so you can reconfigure from "home LAN +
  kiwi-node" to "tethering + VPN" *while already on tethering* and then hit Apply —
  one atomic transaction, no window, never a disarm in between.
- **Trusted kiwi-nodes.** A gateway that already tunnels for you, matched on IP **and**
  MAC, can stand in for the VPN on its own network. Opt-in — never inferred.
- **One VPN, enforced.** Only the selected connection's server is reachable. Any other
  tunnel you start simply cannot connect, and the UI names it.
- **Survives everything.** Reboot, logout, login, NetworkManager restarts: the rules
  stay. A separate unit installs a hard block *before the network comes up*.

## Install

Requires `nftables`, `NetworkManager`, `python3-gobject` and (for WireGuard)
`wireguard-tools` — all present on Silverblue/Bluefin. Nothing is written to the
immutable `/usr`.

```bash
git clone https://github.com/derlocke-ng/kiwi-killswitch.git
cd kiwi-killswitch
sudo ./install.sh install     # root daemon, CLI, bus policy, systemd units
./install.sh install          # GNOME extension + settings app
```

Log out and back in so GNOME loads the extension. Your user must be in `wheel` — that is
what the bus policy authorizes.

Via [kiwi-updater](https://github.com/derlocke-ng/kiwi-updater), once it exists:
`kiwi install kiwi-killswitch` (listed twice: user scope and `scope=system`).

Uninstall with `sudo ./install.sh uninstall` — it disarms first, so it cannot leave you
locked out.

## Use

**Quick Settings** → *Kill Switch* to arm, disarm and pick which VPN is protected.
**Kiwi Kill Switch** in the app grid for everything else — the connection list is on
its front page.

```bash
kiwi-killswitch status                   # what is enforced right now
kiwi-killswitch list                     # VPN connections NM knows about
kiwi-killswitch vpn lime                 # stage which one is protected
kiwi-killswitch set mgmt_subnets 192.168.2.0/24
kiwi-killswitch set dns_mode tunnel
kiwi-killswitch apply                    # one atomic transaction
kiwi-killswitch arm                      # or: arm <vpn>
kiwi-killswitch disarm
```

Settings are **staged** until you apply. `kiwi-killswitch pending` shows what is queued,
`revert` discards it. Turn staging off with `set instant_apply 1` if you prefer.

### Before you arm on a machine you reach over SSH

Arming pins the established-connections rule to the exit interface, so a session from a
subnet that is not in `mgmt_subnets` **will be cut**. That is correct kill-switch
behaviour. Set it first:

```bash
kiwi-killswitch set mgmt_subnets 192.168.0.0/16
kiwi-killswitch set allow_in_ports 22
kiwi-killswitch apply
```

### Virtual machines

A **bridged** VM has its own address on the LAN and never routes through the
host, so the kill switch does not touch it — at `standard` or `strict`. Avoid
`paranoid`: that layer filters at the physical card, which is where the VM's
frames leave, so they would be dropped.

A **NAT'd** VM routes through the host. `strict` (the default) forwards it out
the tunnel and blocks it when the tunnel is down — which is usually what you
want. Add its subnet (e.g. `192.168.122.0/24`) to `mgmt_subnets` as well, so its
DHCP and DNS can reach the host: those arrive *inbound*, and `bypass_subnets` /
`allow_lan` are outbound-only.

| setting | outbound | inbound | pinned off the tunnel |
|---|---|---|---|
| `allow_lan` | yes (auto) | no | no |
| `bypass_subnets` | yes | no | no |
| `mgmt_subnets` | yes | yes | yes |

### Recovery

The CLI needs no network, so a TTY is always enough:

```bash
kiwi-killswitch disarm
```

If the daemon itself is gone:

```bash
sudo nft delete table inet kiwi_ks
sudo nft delete table netdev kiwi_ks_egress
```

## Verifying it is leak-proof

```bash
kiwi-killswitch arm
curl -s ifconfig.me                       # your VPN's address
sudo nmcli connection down <your-vpn>     # simulate a drop
curl -s --max-time 5 ifconfig.me          # must TIME OUT, not show your real IP
sudo nft list table inet kiwi_ks          # counters named ks-drop-* show what was blocked
```

Do **not** judge this by comparing exit IPs. If your LAN already egresses through the
same gateway as your VPN, a tunnelled and an untunnelled request return the same
address. Prove tunnel use with `wg show <iface> transfer` before and after, or
`ip route get 1.1.1.1`. The full sequence is in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Coexistence

**firewalld can stay on.** Its chains run at priority 10; ours at −10. A `drop` is
terminal across base chains while an `accept` only ends the current one, so the kill
switch blocks what it must and firewalld still filters everything it lets through.
`firewall-cmd --reload` does not remove our tables, and if anything ever does, the
daemon re-asserts them within seconds.

Do **not** run this alongside another kill switch (`nova-killswitch`, `nova-vpn`). Two
processes owning an output-drop table will fight.

## Security notes

- Passwordless control is the D-Bus **bus policy** for group `wheel`
  ([data/dbus](data/dbus/eu.kiwinetwork.KillSwitch.conf)) — not a pkexec rule, and not
  sudoers. The daemon exposes a fixed set of validated methods, never "run this as root".
  Don't loosen it.
- **Trusted-node MAC binding** raises the bar against a hostile network handing out your
  router's IP. It does not stop an attacker already on the wire from forging a MAC.
  A deliberate tradeoff.
- **Paranoid hardening is provisional on first apply.** A wrong allow-rule in the netdev
  chain takes the machine fully offline, so it reverts itself unless you confirm.
- This is not Tails or Whonix. It stops network-level leaks; it does nothing about
  browser fingerprinting, WebRTC, or an application that deliberately phones home
  through the tunnel.
- Personal tool. Not independently audited.

Derived from the author's `gnome-vpn-killswitch` (boot ordering), `kiwi-vpn-monitor`
(gateway intelligence) and `nova-killswitch` (the daemon architecture and its
hard-won leak fixes — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

## License

GPL-3.0-or-later.
