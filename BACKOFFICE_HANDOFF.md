# Backoffice Handoff

Updated: 2026-09-16

## Current State

- The main member app remains on the existing Flask app and lottery room page.
- A separate Flask backoffice app exists in `backoffice_app.py`.
- Local backoffice preview runs on port `5004` and is reachable on the LAN at `http://192.168.1.164:5004/login`.
- The backoffice shares the main app database, `SECRET_KEY`, users, roles, and session format.
- Admin and active Partner accounts can log in to the separate backoffice.
- Admin and Partner dashboards are role-scoped:
  - Admin sees system-wide users, rooms, pending deposits/withdrawals, announcements, and recent accounts.
  - Partner sees only their own member line, credit balance, commission rate, online members, betting summary, commission entries, and wallet history.
- The Admin sidebar is collapsible and stores its state in localStorage.
- The existing mobile lottery page still has the current desktop-scale viewport work in `templates/lottery_rooms.html`.
- The standalone backoffice now proxies main-app Admin/Partner routes under `/main/...`, keeping navigation on port `5004` while reusing the real handlers and shared database.
- Verified Admin pages through the proxy: `/main/admin/lottery-rooms` and `/main/admin/wallet`.

## Files Added

- `backoffice_app.py`
- `templates/backoffice_login.html`
- `templates/backoffice_standalone.html`
- `templates/backoffice_partner.html`
- `templates/backoffice_admin.html`
- `templates/backoffice_admin_collapsible.html`

## Important Runtime Commands

```powershell
python -c "from backoffice_app import backoffice_app; backoffice_app.run(host='0.0.0.0', port=5004, debug=False)"
```

Mobile login URL:

`http://192.168.1.164:5004/login`

## Current Limitation

- Proxied operation pages are still rendered by the main app handlers, but they are now same-origin under the standalone backoffice and keep the shared Admin/Partner session.
- Verify all POST actions and Partner navigation through the proxy before removing old Admin/Partner buttons from the member site.
- Do not hide the old Admin/Partner buttons yet.
- The internal proxy must be restarted after code changes because the preview server runs with debug disabled.

## Next Work Order

1. Verify POST actions and Partner navigation through the same-origin proxy.
2. Add role-specific action guards and tests for Admin versus Partner data visibility.
3. Verify shared login, session, database writes, and mobile access.
4. Only after those checks pass, hide the old Admin and Partner buttons from the main site.

## Validation Already Completed

- `backoffice_app.py` imports successfully.
- Backoffice Login returned HTTP 200.
- Unauthenticated Dashboard redirected HTTP 302 to Login.
- Existing lottery page continued returning HTTP 200.
- Partner Dashboard loaded real data during browser verification.
- Admin Dashboard loaded real data and its collapse button changed from `ยุบเมนู` to `ขยายเมนู` during browser verification.
- Admin room management loaded at `http://127.0.0.1:5004/main/admin/lottery-rooms`.
- Admin wallet queue loaded at `http://127.0.0.1:5004/main/admin/wallet`.
