# Changelog

Releases of kiwi-killswitch. The newest is on top; its heading is the
version in kiwi.manifest.

## 0.2.5 — 2026-11-21

- **GNOME 51.** The Quick Settings toggle declares GNOME Shell 51; GNOME
  showed it as out of date and did not load it. It uses none of the APIs
  GNOME 51 removed or changed, so the code itself is the same.
- **Signed releases.** Release tags are signed with the Kiwi release key
  (`.github/allowed_signers`), and the release workflow only publishes a
  signed tag; it no longer makes tags itself. kiwi updates the system half
  (the root daemon) without a password only to a signed release. Earlier
  releases were unsigned, so the first update to 0.2.5 still asks for your
  password once: kiwi then trusts the key, and later updates need none.
