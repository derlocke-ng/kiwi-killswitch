# kiwi-killswitch — verification runbook

The sequence below is what was actually run against a Bluefin 44 test machine.
Every check states what the *correct* answer is, because several of them look
like failures when they are working.

## Before you start

**Set the admin path first, or arming will cut your session.** The established
rule is pinned to the exit interface, so an SSH connection from a subnet that is
not in `mgmt_subnets` is dropped the moment you arm. That is correct
kill-switch behaviour; it is also how you lock yourself out of a remote box.

```bash
kiwi-killswitch set mgmt_subnets 192.168.0.0/16
kiwi-killswitch set allow_in_ports 22
kiwi-killswitch apply
```

**Put a dead-man switch on a remote machine** before the first arm:

```bash
sudo systemd-run --on-active=300 --unit=ks-panic \
    /bin/sh -c 'systemctl stop kiwi-killswitchd; nft delete table inet kiwi_ks; \
                nft delete table netdev kiwi_ks_egress'
# cancel once you are satisfied:
sudo systemctl stop ks-panic.timer
```

Do **not** leave stale `sleep N; disarm` loops running. A forgotten one firing
mid-test produces a convincing false "leak" reading, because a disarm really does
restore the real address.

## 1. It is loaded and honest

```bash
kiwi-killswitch status          # armed=true, enforcing=true
sudo nft list table inet kiwi_ks
sudo nft list table netdev kiwi_ks_egress   # paranoid only
```

`armed` is intent; `enforcing` is read from the kernel on every status call. If
they ever disagree the UI must say `NOT PROTECTED` — that state is the dangerous
one, because it looks safe.

## 2. Fail-closed with the VPN down

Arm with no VPN running. All of these must fail, and the admin path must survive:

```bash
curl -s --max-time 6 https://ifconfig.me     # must time out
ping -c1 -W2 1.1.1.1                          # must fail
getent hosts example.com                      # must fail
ping -c1 -W2 <your admin host>                # must SUCCEED
```

## 3. The tunnel comes up

Bring the VPN up yourself (`nmcli connection up <name>`; over SSH this needs
`sudo`, polkit refuses a non-local session). Within a debounce interval and with
no further action:

```bash
kiwi-killswitch status      # exit_iface = <tunnel>, detail = protected via ...
curl -s https://ifconfig.me # the VPN's address
```

## 4. The leak test

```bash
curl -s ifconfig.me                       # note the address
sudo nmcli connection down <your-vpn>
curl -s --max-time 5 ifconfig.me          # must TIME OUT
```

**Do not judge this by comparing exit IPs.** If your LAN already egresses through
the same gateway as your VPN, a tunnelled and an untunnelled request return the
same address and the test proves nothing. Use interface counters instead:

```bash
sudo wg show <iface> transfer     # before and after
ip route get 1.1.1.1
```

## 5. The tunnel dies at the link level

This is the regression test for the "I have to re-trigger it" class of bug. It is
the *normal* way WireGuard and OpenVPN fail, and it emits **no NetworkManager
signal** — NM and the GNOME panel will both still show the VPN as connected.

```bash
sudo ip link set <tunnel> down
curl -s --max-time 5 ifconfig.me      # must time out, immediately
sleep 5
kiwi-killswitch status
```

Expected: `exit_iface` empty, and detail naming the discrepancy —
`NetworkManager still shows '<vpn>' as connected, but its tunnel interface is
down`. If status still claims `protected via <tunnel>`, the liveness check has
regressed.

## 6. Only one VPN

With the switch armed for VPN A, start VPN B. B must fail to establish (its
endpoints are not permitted), the exit must not move, and status must name it:

```bash
kiwi-killswitch status | grep conflicts
ip -o link show          # B's interface should never appear
```

## 7. DNS

```bash
kiwi-killswitch status | grep dns_path
resolvectl status | grep -E 'Link|DNS Servers|Default Route|DNS Domain'
resolvectl query example.com        # must report  -- link: <tunnel>
sudo nft list table inet kiwi_ks | grep 'dns backstop'
```

Every link other than the chosen one must show `Default Route: no`. Send a packet
at a resolver that is *not* the chosen one and watch the backstop counter rise:

```python
python3 -c "
import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(b'\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01',
         ('<a LAN resolver>',53)); print(s.recv(512))"
```

`PermissionError` is the pass condition.

## 8. A second uplink appears

The wifi-to-5G case. Create one and let it try to take the default route:

```bash
sudo ip link add link <nic> name mvl0 type macvlan mode bridge
sudo ip link set mvl0 up
sudo ip addr add <free LAN address>/16 dev mvl0
sudo ip route add default via <gw> dev mvl0 metric 10   # steal priority
curl -s --max-time 8 --interface mvl0 https://ifconfig.me   # must be blocked
ping -c1 -W2 -I mvl0 1.1.1.1                                # must be blocked
sudo nft list table inet kiwi_ks | grep ks-drop-out         # counter rising
```

Clean up with `sudo ip route del default dev mvl0 metric 10; sudo ip link del mvl0`.

## 9. Forwarded traffic (why `strict` is the default)

```bash
sudo ip netns add kstest
sudo ip link add ks-host type veth peer name ks-guest
sudo ip link set ks-guest netns kstest
sudo ip addr add 10.77.0.1/24 dev ks-host && sudo ip link set ks-host up
sudo ip netns exec kstest ip addr add 10.77.0.2/24 dev ks-guest
sudo ip netns exec kstest ip link set ks-guest up
sudo ip netns exec kstest ip route add default via 10.77.0.1
sudo nft add table ip ksnat
sudo nft add chain ip ksnat post '{ type nat hook postrouting priority 100; }'
sudo nft add rule ip ksnat post ip saddr 10.77.0.0/24 masquerade
sudo sysctl -w net.ipv4.ip_forward=1
```

With the tunnel **down** and the host fully blocked:

| hardening | `ip netns exec kstest curl http://1.1.1.1` |
|---|---|
| `standard` | reaches the internet — the container bypasses the kill switch |
| `strict` | blocked; `ks-drop-fwd` counts it |

With the tunnel **up**, `strict` forwards the guest out through the tunnel and it
shares the host's exit IP. That is intended.

Clean up: `sudo ip netns del kstest; sudo ip link del ks-host; sudo nft delete
table ip ksnat; sudo sysctl -w net.ipv4.ip_forward=0`.

## 10. Raw sockets (why `paranoid` exists)

Write a frame straight to the device, bypassing the IP stack. Use an unrouted
destination such as `198.51.100.7` so nothing is disclosed.

```python
# see docs — a minimal AF_PACKET Ethernet+IP+UDP frame bound to the NIC
```

| hardening | result | `ks-drop-out` | `ks-drop-egress` |
|---|---|---|---|
| `strict` | frame leaves the machine | 0 | (no chain) |
| `paranoid` | `OSError: [Errno 105] No buffer space available` | 0 | rises |

The inet counter staying at 0 is the point: that layer never sees these packets.

## 11. Paranoid is provisional

```bash
kiwi-killswitch set hardening_confirm_sec 20
kiwi-killswitch set hardening paranoid
kiwi-killswitch apply
# wait without confirming
sleep 26
kiwi-killswitch status | grep hardening      # back to strict
```

`kiwi-killswitch confirm` within the window keeps it. Escalating into paranoid
again re-arms the deadline even for a profile confirmed before.

## 12. Someone removes the ruleset

```bash
sudo nft delete table inet kiwi_ks
kiwi-killswitch status      # enforcing=false, "NOT PROTECTED" — immediately
sleep 17
kiwi-killswitch status      # re-asserted
```

## 13. firewalld coexists

```bash
sudo firewall-cmd --reload
sudo nft list table inet kiwi_ks >/dev/null && echo survived
```

Our chains are at priority −10, firewalld's at 10. Both apply.

## 14. Trusted node, and spoofing it

With a node configured and named in `trusted_node`, the LAN uplink becomes the
exit and no VPN is needed. Then point the node's MAC at something wrong:

```bash
kiwi-killswitch status       # node cleared, exit_iface empty, traffic blocked
journalctl -u kiwi-killswitchd | grep MAC
```

## 15. It survives a restart, and a reboot

The daemon restarts on failure and across reboots, so anything it changed on the
running system must be remembered on disk, not in memory:

```bash
kiwi-killswitch arm
sudo systemctl restart kiwi-killswitchd
kiwi-killswitch disarm
resolvectl status <nic> | grep -E 'DNS Servers|Default Route'   # fully restored
curl -s https://ifconfig.me                                      # DNS works
```

A regression here is nasty: DNS breaks system-wide *after* disarming, with
nothing left that knows to undo it.

Reboot while armed and check the ordering:

```bash
journalctl -b -o short-precise -u kiwi-killswitch-boot -u kiwi-killswitchd \
           -u NetworkManager | grep -E 'Starting|Finished|Started'
```

`kiwi-killswitch-boot.service` must **finish** before NetworkManager starts.

## 16. Disarm restores everything

```bash
kiwi-killswitch disarm
sudo nft list tables | grep kiwi          # nothing
resolvectl status <nic> | grep 'Default Route'   # yes
ip route show                                     # no leftover pins
sudo cat /etc/kiwi-killswitch/config.json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["runtime"])'   # both lists empty
curl -s https://ifconfig.me
```

## Recovery

The CLI needs no network, so a TTY is enough:

```bash
kiwi-killswitch disarm
```

If the daemon itself is gone or wedged:

```bash
sudo systemctl stop kiwi-killswitchd
sudo nft delete table inet kiwi_ks
sudo nft delete table netdev kiwi_ks_egress
sudo resolvectl revert <nic> && sudo resolvectl dns <nic> <your resolvers>
```

Clear the persisted intent so a restart does not immediately re-block:

```bash
sudo python3 -c "
import json; p='/etc/kiwi-killswitch/config.json'
d=json.load(open(p)); d['armed']=False; json.dump(d, open(p,'w'), indent=2)"
```
