import GObject from 'gi://GObject';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';

const DEST = 'eu.kiwinetwork.KillSwitch';
const OBJ = '/eu/kiwinetwork/KillSwitch';

// Talk to kiwi-killswitchd over the system bus. Passwordless via its bus
// policy, so there is no pkexec and no authentication dialog. This extension
// is an unprivileged process and never touches the firewall itself — it only
// asks the daemon, which is the whole reason for the split.
//
// Every call is ASYNCHRONOUS. This code runs inside gnome-shell, i.e. inside
// the compositor: a synchronous round trip freezes the entire desktop —
// pointer, animations, everything — for as long as the daemon takes to
// answer, and the daemon answers from the same loop that runs nft and nmcli.
function call(method, params, replyType, cancellable) {
    return new Promise((resolve, reject) => {
        Gio.DBus.system.call(DEST, OBJ, DEST, method, params, replyType,
            Gio.DBusCallFlags.NONE, 15000, cancellable, (conn, res) => {
                try {
                    resolve(conn.call_finish(res));
                } catch (e) {
                    reject(e);
                }
            });
    });
}

function cancelled(e) {
    return e instanceof GLib.Error &&
        e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED);
}

// For a daemon that predates the `state` field — the two halves of this app
// are installed separately, so a new toggle can meet an old daemon.
function legacyState(st) {
    if (!st.armed)
        return 'off';
    if (!st.enforcing)
        return 'unprotected';
    if (st.mode === 'node')
        return 'dns-only';
    return st.exit_iface ? 'protected' : 'blocking';
}

const KillSwitchToggle = GObject.registerClass(
class KillSwitchToggle extends QuickSettings.QuickMenuToggle {
    _init() {
        super._init({
            title: _('Kill Switch'),
            iconName: 'changes-allow-symbolic',
            toggleMode: true,
        });

        this._busy = false;
        this._warned = false;
        this._gone = false;
        this._serial = 0;
        this._menuKey = null;
        this._cancellable = new Gio.Cancellable();

        this.menu.setHeader('network-vpn-symbolic', _('Kiwi Kill Switch'));
        this._vpns = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._vpns);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction(_('Settings…'), () => this._openSettings());

        this.connect('clicked', () => this._onClicked());
        this.connect('destroy', () => this._onDestroy());

        // live updates: the daemon emits Changed when its answer changes
        this._sigId = Gio.DBus.system.signal_subscribe(
            DEST, DEST, 'Changed', OBJ, null, Gio.DBusSignalFlags.NONE,
            () => this._refresh());

        // ...and it says nothing at all when it stops or comes back — an
        // update restarts it, a crash ends it. Watching the name is what keeps
        // the toggle from showing a state nobody is enforcing any more. The
        // watch also reports the name's presence once right away, which is the
        // first refresh.
        this._watchId = Gio.bus_watch_name(Gio.BusType.SYSTEM, DEST,
            Gio.BusNameWatcherFlags.NONE,
            () => this._refresh(), () => this._refresh());
    }

    _buildVpnList(vpns, current) {
        // Rebuilding the section under an open menu resets hover and focus,
        // so only do it when what it would show is different.
        const key = JSON.stringify([vpns, current]);
        if (key === this._menuKey)
            return;
        this._menuKey = key;
        this._vpns.removeAll();
        if (vpns.length === 0) {
            const item = new PopupMenu.PopupMenuItem(_('No VPN connections'));
            item.setSensitive(false);
            this._vpns.addMenuItem(item);
            return;
        }
        for (const [name, _type, active] of vpns) {
            const label = active === 'yes' ? `${name}  ✓` : name;
            const item = new PopupMenu.PopupMenuItem(label);
            // Arm(vpn) both selects and enforces, so picking one here is a
            // complete action rather than a staged one — staging belongs in
            // the settings app, where you can review a batch before applying.
            item.connect('activate', () => this._act('Arm',
                new GLib.Variant('(s)', [name])));
            item.setOrnament(name === current
                ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
            this._vpns.addMenuItem(item);
        }
    }

    _openSettings() {
        try {
            Gio.Subprocess.new(['kiwi-killswitch-settings'], Gio.SubprocessFlags.NONE);
        } catch (e) {
            Main.notify(_('Kiwi Kill Switch'), _('Settings app is not installed.'));
        }
    }

    _onClicked() {
        this._act(this.checked ? 'Arm' : 'Disarm',
            this.checked ? new GLib.Variant('(s)', ['']) : null);
    }

    async _act(method, params = null) {
        if (this._busy)
            return;
        this._busy = true;
        this.reactive = false;
        this.subtitle = method === 'Disarm' ? _('Turning off…') : _('Arming…');
        try {
            await call(method, params, null, this._cancellable);
        } catch (e) {
            if (cancelled(e))
                return;
            Main.notify(_('Kiwi Kill Switch'), e.message);
        }
        if (this._gone)
            return;
        this._busy = false;
        this.reactive = true;
        this._refresh();
    }

    async _refresh() {
        // Signals can arrive faster than answers. Only the newest request may
        // paint, or an old reply could overwrite a newer state.
        const serial = ++this._serial;
        let st = null;
        let vpns = [];
        try {
            const r = await call('GetStatus', null,
                new GLib.VariantType('(a{sv})'), this._cancellable);
            const dict = r.deepUnpack()[0];
            st = {};
            for (const k in dict)
                st[k] = dict[k].deepUnpack();
            const v = await call('ListVpns', null,
                new GLib.VariantType('(a(sss))'), this._cancellable);
            vpns = v.deepUnpack()[0];
        } catch (e) {
            if (cancelled(e))
                return;
        }
        if (this._gone || serial !== this._serial)
            return;
        this._render(st, vpns);
    }

    _render(st, vpns) {
        if (st === null) {
            this.subtitle = _('daemon off');
            this.iconName = 'changes-allow-symbolic';
            this._buildVpnList([], '');
            return;
        }
        const state = st.state ?? legacyState(st);
        if (!this._busy)
            this.checked = state !== 'off';
        this._buildVpnList(vpns, st.vpn || '');

        let sub;
        switch (state) {
        case 'off':
            this.iconName = 'changes-allow-symbolic';
            this.subtitle = _('Off');
            this._warned = false;
            return;
        case 'unprotected':
            // Armed but no ruleset loaded: the dangerous state, because it
            // looks protected while traffic is unfiltered. Say so loudly —
            // silence here is how a leak goes unnoticed.
            this.iconName = 'dialog-error-symbolic';
            this.subtitle = _('NOT PROTECTED');
            if (!this._warned) {
                this._warned = true;
                Main.notify(_('Kiwi Kill Switch'),
                    _('Armed, but no firewall is loaded — your traffic is NOT protected. Check: journalctl -u kiwi-killswitchd'));
            }
            return;
        case 'blocking':
            this.iconName = 'dialog-warning-symbolic';
            sub = _('Blocking everything');
            break;
        case 'dns-only':
            // Node mode leaves the internet open: never the closed padlock,
            // never a word that reads as "safe".
            this.iconName = 'changes-allow-symbolic';
            sub = _('DNS only — not protected');
            break;
        default:
            this.iconName = 'changes-prevent-symbolic';
            sub = st.detail || _('Protected');
        }
        this._warned = false;
        if (st.pending && Number(st.pending) > 0)
            sub += _(' · unapplied changes');
        this.subtitle = sub;
    }

    _onDestroy() {
        this._gone = true;
        this._cancellable.cancel();
        if (this._sigId) {
            Gio.DBus.system.signal_unsubscribe(this._sigId);
            this._sigId = 0;
        }
        if (this._watchId) {
            Gio.bus_unwatch_name(this._watchId);
            this._watchId = 0;
        }
    }
});

const Indicator = GObject.registerClass(
class Indicator extends QuickSettings.SystemIndicator {
    _init() {
        super._init();
        this._indicator = this._addIndicator();
        this._indicator.icon_name = 'changes-allow-symbolic';
        this._toggle = new KillSwitchToggle();
        this._toggle.bind_property('checked', this._indicator, 'visible',
            GObject.BindingFlags.SYNC_CREATE);
        this._toggle.bind_property('icon-name', this._indicator, 'icon-name',
            GObject.BindingFlags.SYNC_CREATE);
        this.quickSettingsItems.push(this._toggle);
    }
});

export default class KiwiKillSwitchExtension extends Extension {
    enable() {
        this._indicator = new Indicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator.quickSettingsItems.forEach(i => i.destroy());
        this._indicator.destroy();
        this._indicator = null;
    }
}
