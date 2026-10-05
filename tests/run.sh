#!/usr/bin/env bash
# Regression suite for kiwi-killswitchd.
#
#   tests/run.sh             everything
#   tests/run.sh mgmt arp    just those groups
#   tests/run.sh gui         the settings app, on a private headless display
#
# Needs no root and changes nothing on this machine: every group runs in its
# own throwaway user + network + mount namespace, with the real daemon code,
# real nft and real routing inside it. Run it after ANY change to the daemon.
#
# It is not a substitute for docs/RUNBOOK.md — NetworkManager, resolved and
# real hardware are not in here.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
all=(validate endpoints nm rulesets failclosed inject mgmt net6 arp classify fwd churn monitor dbus gui)
groups=("$@")
(( ${#groups[@]} )) || groups=("${all[@]}")

for t in unshare nft ip python3; do
    command -v "$t" >/dev/null 2>&1 || { echo "missing: $t" >&2; exit 2; }
done

ok=0 fail=0 broken=()
for g in "${groups[@]}"; do
    state="$(mktemp -d)"
    wrap=""
    if [[ $g == gui ]]; then
        # Not in a namespace: it needs a display. It gets its own — a headless
        # compositor on a private session bus — so no window appears on the
        # real desktop and nothing here can reach the real session.
        if ! command -v mutter >/dev/null 2>&1 || ! command -v dbus-run-session >/dev/null 2>&1; then
            echo "== gui"; echo "  skip  (needs mutter and dbus-run-session)"; rm -rf "$state"; continue
        fi
        out="$(GUI_RESULT="$state/gui" timeout 90 dbus-run-session -- mutter --headless --wayland \
            --no-x11 --wayland-display "ks-test-$$" --virtual-monitor 900x1000 -- \
            python3 -B "$here/gui_smoke.py" 2>/dev/null)"
        grep -E '^(== |  )' <<<"$out"
        o=$(grep -c '^  ok ' <<<"$out"); f=$(grep -c '^  FAIL' <<<"$out")
        ok=$((ok + o)); fail=$((fail + f))
        if [[ ! -s $state/gui ]] || (( f )); then broken+=("gui"); fi
        rm -rf "$state"; continue
    fi
    if [[ $g == dbus ]]; then
        if command -v dbus-run-session >/dev/null 2>&1; then
            wrap="dbus-run-session --"
        else
            echo "== dbus"; echo "  skip  (dbus-run-session not installed)"; continue
        fi
    fi
    # shellcheck disable=SC2016  # expanded by the inner shell, on purpose
    LAB_DIR="$state" HOME="$state" unshare -Urnm bash -c '
        mount -t sysfs sysfs /sys 2>/dev/null
        ip link set lo up
        exec $2 python3 -B "$0/lab.py" "$1"' "$here" "$g" "$wrap" \
        2> >(grep -v "Cannot set up inotify" >&2)   # dbus-daemon, harmless in a userns
    rc=$?
    if [[ -f $state/result ]]; then
        read -r o f < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["ok"], d["fail"])' "$state/result")
        ok=$((ok + o)); fail=$((fail + f))
    else
        rc=1
    fi
    (( rc )) && broken+=("$g")
    rm -rf "$state"
done

echo
if (( ${#broken[@]} )); then
    echo "FAILED: ${broken[*]}   ($ok passed, $fail failed)"
    exit 1
fi
echo "all good: $ok checks passed"
