#!/usr/bin/env bash
# installer for kiwi-killswitch — DUAL SCOPE.
#
# Works standalone (just run it) and under kiwi-updater, which invokes it as
#   ./install.sh install|update|uninstall
# with KIWI_SCOPE / KIWI_PREFIX / KIWI_APP_DIR / KIWI_ACTION in the environment.
#
#   KIWI_SCOPE=system  -> root backend: daemon, CLI, bus policy, systemd units
#   KIWI_SCOPE=user    -> GNOME extension + GTK settings app (skipped headless)
#
# `uninstall --purge` (or KIWI_PURGE=1) also removes the saved settings. A plain
# uninstall keeps them, so a reinstall picks up where you left off.
#
# The system scope needs root. Run it with sudo; it does not escalate by itself.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-${KIWI_ACTION:-install}}"
SCOPE="${KIWI_SCOPE:-}"
PURGE="${KIWI_PURGE:-0}"
[[ " $* " == *" --purge "* ]] && PURGE=1

EXT_UUID="kiwi-killswitch@kiwi-network.eu"
BUS_NAME="eu.kiwinetwork.KillSwitch"

say()  { printf ':: %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# With no explicit scope, do the sensible thing for whoever is running us.
if [[ -z $SCOPE ]]; then
    if [[ $EUID -eq 0 ]]; then SCOPE=system; else SCOPE=user; fi
fi

want_gui() {
    [[ ${KIWI_GUI:-} == 0 ]] && return 1
    [[ -n ${KIWI_GUI:-} ]] && return 0
    [[ -e /usr/lib64/girepository-1.0/Gtk-4.0.typelib ||
       -e /usr/lib/girepository-1.0/Gtk-4.0.typelib ]]
}

# ---------------- system scope (root): the daemon ---------------------------
sys_preflight() {
    [[ $EUID -eq 0 ]] || die "system scope needs root — try: sudo $0 $ACTION"
    local missing=()
    for t in nft ip nmcli python3; do
        command -v "$t" >/dev/null 2>&1 || missing+=("$t")
    done
    (( ${#missing[@]} )) && die "missing required tools: ${missing[*]}"
    python3 -c 'import gi; gi.require_version("Gio","2.0")' 2>/dev/null \
        || die "python3 PyGObject (gi) is required"
    command -v wg        >/dev/null 2>&1 || warn "wg not found — WireGuard endpoint detection will be limited"
    command -v resolvectl >/dev/null 2>&1 || warn "resolvectl not found — DNS steering will be skipped"
    return 0
}

sys_install() {
    sys_preflight
    say "installing kiwi-killswitchd (root daemon) + D-Bus service"
    install -Dm755 "$SRC/daemon/kiwi-killswitchd" /usr/local/sbin/kiwi-killswitchd
    install -Dm755 "$SRC/bin/kiwi-killswitch"     /usr/local/bin/kiwi-killswitch
    install -Dm644 "$SRC/data/dbus/$BUS_NAME.conf" \
        "/etc/dbus-1/system.d/$BUS_NAME.conf"
    install -Dm644 "$SRC/data/systemd/kiwi-killswitchd.service" \
        /etc/systemd/system/kiwi-killswitchd.service
    install -Dm644 "$SRC/data/systemd/kiwi-killswitch-boot.service" \
        /etc/systemd/system/kiwi-killswitch-boot.service
    install -dm755 /etc/kiwi-killswitch
    [[ -f /etc/kiwi-killswitch/config.json ]] || \
        printf '{}\n' > /etc/kiwi-killswitch/config.json

    # The system bus must reload to honour the new policy (passwordless wheel).
    # ReloadConfig is the canonical trigger and works for dbus-daemon AND
    # dbus-broker; the systemctl calls are only a fallback for odd setups.
    busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
        ReloadConfig 2>/dev/null \
        || systemctl reload dbus-broker 2>/dev/null \
        || systemctl reload dbus 2>/dev/null || true

    systemctl daemon-reload
    systemctl enable kiwi-killswitch-boot.service 2>/dev/null || true
    systemctl enable kiwi-killswitchd.service 2>/dev/null || true
    systemctl restart kiwi-killswitchd.service

    # Name ownership is asynchronous — returning before it lands makes the very
    # first CLI or GUI call fail for no visible reason. Wait for a real method
    # call to succeed, not merely for the name to appear: those are different
    # moments, and only the second one means the daemon is usable.
    local i ready=0
    for i in $(seq 40); do
        if kiwi-killswitch status >/dev/null 2>&1; then ready=1; break; fi
        sleep 0.25
    done
    if (( ! ready )); then
        warn "the daemon is not answering on $BUS_NAME — check: journalctl -u kiwi-killswitchd"
    fi

    if systemctl is-active --quiet firewalld; then
        say "firewalld is active — that is fine. Our chains run at priority -10,"
        say "ahead of firewalld's at 10, so both apply."
    fi
    say "installed. Control it from the GNOME toggle, the settings app, or:"
    say "  kiwi-killswitch status | list | vpn <name> | apply | arm | disarm"
}

sys_update() { sys_install; }

sys_uninstall() {
    [[ $EUID -eq 0 ]] || die "system scope needs root — try: sudo $0 uninstall"
    # Disarm FIRST. Removing the daemon while armed leaves the machine blocked
    # with the tool that unblocks it already deleted.
    say "disarming before removal"
    if ! kiwi-killswitch disarm >/dev/null 2>&1; then
        # The daemon is not answering, so nothing has undone what it changed on
        # this machine: resolver overrides, management rules, the armed flag.
        # Deleting the tables alone would leave DNS steered at a tunnel that is
        # about to be unreachable. Its panic path does the whole teardown
        # without needing the bus.
        if [[ -x /usr/local/sbin/kiwi-killswitchd ]]; then
            /usr/local/sbin/kiwi-killswitchd --panic >/dev/null 2>&1 || true
        fi
    fi
    nft delete table inet kiwi_ks 2>/dev/null || true
    nft delete table netdev kiwi_ks_egress 2>/dev/null || true

    systemctl disable --now kiwi-killswitchd.service 2>/dev/null || true
    systemctl disable --now kiwi-killswitch-boot.service 2>/dev/null || true
    rm -f /usr/local/sbin/kiwi-killswitchd /usr/local/bin/kiwi-killswitch \
          "/etc/dbus-1/system.d/$BUS_NAME.conf" \
          /etc/systemd/system/kiwi-killswitchd.service \
          /etc/systemd/system/kiwi-killswitch-boot.service \
          /run/kiwi-killswitch.nft /run/kiwi-killswitch-boot.nft
    busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
        ReloadConfig 2>/dev/null || true
    systemctl daemon-reload
    if [[ $PURGE == 1 ]]; then
        rm -rf /etc/kiwi-killswitch
        say "removed, settings included (--purge)."
    else
        say "removed. Kept /etc/kiwi-killswitch (your settings) — uninstall with --purge to drop them too."
    fi
}

# ---------------- user scope ------------------------------------------------
user_install() {
    if ! want_gui; then
        say "no GTK4/GNOME stack (or KIWI_GUI=0) — skipping extension + settings app"
        return
    fi
    local ext_dir="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$EXT_UUID"
    say "installing GNOME extension -> $ext_dir"
    mkdir -p "$ext_dir"
    cp "$SRC/extension/extension.js" "$SRC/extension/metadata.json" "$ext_dir/"

    say "installing the settings app"
    install -Dm755 "$SRC/gui/kiwi-killswitch-settings" \
        "$HOME/.local/bin/kiwi-killswitch-settings"
    install -Dm644 "$SRC/data/kiwi-killswitch.desktop" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/applications/eu.kiwinetwork.KiwiKillSwitch.desktop"

    command -v gnome-extensions >/dev/null 2>&1 && \
        gnome-extensions enable "$EXT_UUID" 2>/dev/null || true

    id -nG "$USER" | tr ' ' '\n' | grep -qx wheel || \
        warn "$USER is not in the 'wheel' group — the daemon's bus policy will deny it. Fix: sudo usermod -aG wheel $USER"

    say "installed — log out and back in (or Alt+F2 'r' on X11) to load the extension"
}

user_update() { user_install; }

user_uninstall() {
    local ext_dir="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$EXT_UUID"
    command -v gnome-extensions >/dev/null 2>&1 && \
        gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
    rm -rf "$ext_dir"
    rm -f "$HOME/.local/bin/kiwi-killswitch-settings" \
          "${XDG_DATA_HOME:-$HOME/.local/share}/applications/eu.kiwinetwork.KiwiKillSwitch.desktop"
    say "extension + settings app removed"
}

case "$ACTION" in
    install|update|uninstall) ;;
    *) die "usage: $0 {install|update|uninstall [--purge]}   (KIWI_SCOPE=system|user)" ;;
esac

case "$SCOPE" in
    system) "sys_$ACTION" ;;
    user)   "user_$ACTION" ;;
    *) die "unknown KIWI_SCOPE: $SCOPE" ;;
esac
