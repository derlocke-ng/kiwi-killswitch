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
function callSync(method, params = null, replyType = null) {
    return Gio.DBus.system.call_sync(DEST, OBJ, DEST, method, params, replyType,
        Gio.DBusCallFlags.NONE, 5000, null);
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

        this.menu.setHeader('network-vpn-symbolic', _('Kiwi Kill Switch'));
        this._vpns = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._vpns);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction(_('Settings…'), () => this._openSettings());

        this.connect('clicked', () => this._onClicked());
        this.connect('destroy', () => this._onDestroy());

        // live updates: the daemon emits Changed on every state change
        this._sigId = Gio.DBus.system.signal_subscribe(
            DEST, DEST, 'Changed', OBJ, null, Gio.DBusSignalFlags.NONE,
            () => this._refresh());

        this._refresh();
    }

    _buildVpnList(current) {
        this._vpns.removeAll();
        let vpns = [];
        try {
            vpns = callSync('ListVpns', null,
                new GLib.VariantType('(a(sss))')).deepUnpack()[0];
        } catch (e) {
            vpns = [];
        }
        if (vpns.length === 0) {
            const item = new PopupMenu.PopupMenuItem(_('No VPN connections'));
            item.setSensitive(false);
            this._vpns.addMenuItem(item);
            return;
        }
        for (const [name, type, active] of vpns) {
            const label = active === 'yes' ? `${name}  ✓` : name;
            const item = new PopupMenu.PopupMenuItem(label);
            // Arm(vpn) both selects and enforces, so picking one here is a
            // complete action rather than a staged one — staging belongs in
            // the settings app, where you can review a batch before applying.
            item.connect('activate', () => this._call('Arm',
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
        if (this._busy)
            return;
        this._call(this.checked ? 'Arm' : 'Disarm',
            this.checked ? new GLib.Variant('(s)', ['']) : null);
    }

    _call(method, params = null) {
        this._busy = true;
        this.reactive = false;
        try {
            callSync(method, params, null);
        } catch (e) {
            Main.notify(_('Kiwi Kill Switch'), e.message);
        }
        this._busy = false;
        this.reactive = true;
        this._refresh();
    }

    _refresh() {
        let st = {};
        try {
            const r = callSync('GetStatus', null, new GLib.VariantType('(a{sv})'));
            const dict = r.deepUnpack()[0];
            for (const k in dict)
                st[k] = dict[k].deepUnpack();
        } catch (e) {
            this.subtitle = _('daemon off');
            this.iconName = 'changes-allow-symbolic';
            this._buildVpnList('');
            return;
        }

        const armed = st.armed === true || st.armed === 'true';
        const enforcing = st.enforcing === true || st.enforcing === 'true';

        this._syncing = true;
        this.checked = armed;
        this._syncing = false;
        this._buildVpnList(st.vpn || '');

        if (!armed) {
            this.iconName = 'changes-allow-symbolic';
            this.subtitle = _('Off');
            this._warned = false;
            return;
        }

        if (!enforcing) {
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
        }

        this._warned = false;
        const blocked = !st.exit_iface && st.mode !== 'node';
        this.iconName = blocked ? 'dialog-warning-symbolic' : 'changes-prevent-symbolic';
        let sub = st.detail || _('Protected');
        if (st.pending && Number(st.pending) > 0)
            sub += _(' · unapplied changes');
        this.subtitle = sub;
    }

    _onDestroy() {
        if (this._sigId) {
            Gio.DBus.system.signal_unsubscribe(this._sigId);
            this._sigId = 0;
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
