"""AeroFoil's own pages for Lantern: the approval page of a pairing, and the devices of a user.

A device asks to be paired and shows a short code; the user, signed in to AeroFoil, types or follows it here and
approves (SPEC.md § 5.2). The page shows the server's key fingerprint to compare with the device's, and a form
protected by a token from the user's session, so another site cannot approve a device for them.
"""

import html
import secrets

from flask import Blueprint, jsonify, redirect, request, session, url_for
from flask_login import current_user, login_required

from app.lantern_native import PAIR_PATH
from app.lantern_native import accounts as mirror_accounts
from app.lantern_native import static_export

ATTEMPTS = 10
WINDOW_S = 300


def _page(body, status=200):
    text = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer"><title>Pair a device</title>'
        '<style>body{font:16px system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem}'
        'code{font-size:1.1em}button{font:inherit;padding:.5rem 1rem;margin-right:.5rem}</style></head><body>'
        f'<h1>Pair a device</h1>{body}</body></html>'
    )
    return text, status, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'}


def _token():
    token = session.get('lantern_csrf')
    if not token:
        token = secrets.token_urlsafe(24)
        session['lantern_csrf'] = token
    return token


def _fingerprint(lantern):
    from lantern.spki import short

    return short(lantern.spki) if lantern.spki else None


def _code_form(lantern, message=''):
    note = f'<p>{html.escape(message)}</p>' if message else ''
    return _page(
        f'{note}<form method="get" action="{PAIR_PATH}"><label>Code '
        '<input name="code" autocomplete="off" autofocus></label> <button>Continue</button></form>'
    )


def blueprint_for(lantern, mirror):
    """The pages bound to a running LanternApp and its account mirror."""
    from lantern.server.pairing import Limiter

    blueprint = Blueprint('lantern_native', __name__)
    limiter = Limiter(ATTEMPTS, WINDOW_S)

    def lookup(code):
        wait = limiter.wait(request.remote_addr or '')
        if wait:
            return None, _page('<p>Too many tries. Wait a few minutes.</p>', 429)
        entry = lantern.desk.by_code(code) if code else None
        if code and entry is None:
            limiter.record(request.remote_addr or '')
        return entry, None

    @blueprint.route(PAIR_PATH, methods=['GET', 'POST'])
    def pair():
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))
        if not current_user.has_shop_access():
            return _page('<p>Your account cannot use Lantern.</p>', 403)
        code = (request.values.get('code') or '').strip()[:64]
        if not code:
            return _code_form(lantern)
        entry, refused = lookup(code)
        if refused is not None:
            return refused
        if entry is None:
            return _code_form(lantern, 'That code is not waiting for approval (it lives 10 minutes).')
        if request.method == 'POST':
            if not secrets.compare_digest(request.form.get('csrf', ''), session.get('lantern_csrf', '-')):
                return _page('<p>This page expired. Go back and try again.</p>', 400)
            if request.form.get('decision') == 'approve':
                name = mirror_accounts.ensure_account(lantern.accounts, current_user)
                mirror.remember(name)
                granted = lantern.desk.approve(entry, name)
                shown = html.escape(', '.join(granted) or 'nothing')
                return _page(f'<p>Approved <strong>{html.escape(entry.device)}</strong> as {html.escape(name)}: {shown}.</p>')
            lantern.desk.refuse(entry)
            return _page('<p>Refused.</p>')
        wanted = html.escape(', '.join(entry.scopes) or 'nothing')
        fingerprint = _fingerprint(lantern)
        key = (
            f'<p>This server\'s key: <code>{html.escape(fingerprint)}</code>. Compare it with the one the device shows.</p>'
            if fingerprint
            else ''
        )
        return _page(
            f'{key}<p>The device <strong>{html.escape(entry.device)}</strong> asks for: {wanted}.</p>'
            f'<p>Its code: <code>{html.escape(entry.code)}</code>. Approve only a device you are holding.</p>'
            f'<form method="post" action="{PAIR_PATH}"><input type="hidden" name="code" value="{html.escape(code)}">'
            f'<input type="hidden" name="csrf" value="{html.escape(_token())}">'
            '<button name="decision" value="approve">Approve</button>'
            '<button name="decision" value="refuse">Refuse</button></form>'
        )

    def device_json(device):
        return {
            'id': device.id,
            'device': device.device,
            'scopes': list(device.scopes),
            'created': device.created,
            'last_used': device.last_used,
            'user': device.user,
        }

    @blueprint.get('/api/lantern-native/devices')
    @login_required
    def devices():
        mirror.sync()
        mine = mirror_accounts.lantern_name(current_user.user)
        owner = None if (current_user.has_admin_access() and request.args.get('all')) else mine
        return jsonify({'success': True, 'devices': [device_json(d) for d in lantern.accounts.devices(owner)]})

    @blueprint.delete('/api/lantern-native/devices/<device_id>')
    @login_required
    def revoke(device_id):
        mine = mirror_accounts.lantern_name(current_user.user)
        found = next((d for d in lantern.accounts.devices(None) if d.id == device_id), None)
        if found is None or (found.user != mine and not current_user.has_admin_access()):
            return jsonify({'success': False, 'error': 'No such device.'}), 404
        lantern.accounts.revoke(device_id)
        return jsonify({'success': True})

    exporter = static_export.Exporter(lantern)

    def admin_only():
        if not current_user.has_admin_access():
            return jsonify({'success': False, 'error': 'Administrators only.'}), 403
        return None

    @blueprint.get('/api/lantern-native/export')
    @login_required
    def export_status():
        return admin_only() or jsonify({'success': True, 'export': exporter.status()})

    @blueprint.post('/api/lantern-native/export')
    @login_required
    def export_start():
        refused = admin_only()
        if refused is not None:
            return refused
        if not request.is_json:  # a form from another site cannot send this
            return jsonify({'success': False, 'error': 'Send JSON.'}), 415
        name = str((request.get_json(silent=True) or {}).get('name') or 'library').strip()
        try:
            started = exporter.start(name)
        except ValueError as error:
            return jsonify({'success': False, 'error': str(error)}), 400
        if not started:
            return jsonify({'success': False, 'error': 'An export is already running.'}), 409
        return jsonify({'success': True, 'export': exporter.status()}), 202

    return blueprint
