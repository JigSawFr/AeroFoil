"""Lantern requests for games, kept as AeroFoil's own title requests.

AeroFoil keeps one request per title, shared by every user who asked for it (`TitleRequests` and
`TitleRequestUsers`). A Lantern request is one user's part of it: it is created through AeroFoil's own function,
shows in AeroFoil's Requests page, and is answered there (closed or denied) or through the administration API.

- Statuses: open is open, closed (the title came to the library) is done, denied is declined.
- A request has the id Lantern gave it when it was made, kept in `config/lantern-request-ids.db`; one made on
  AeroFoil's page has the id `r_<request>-<user>`. The reason an administrator gave for declining is kept there too
  (AeroFoil has no column for it).
- Withdrawing a request removes the user's part of it; the request itself goes with its last user.
"""

import os
import re
import sqlite3
import threading

from app.constants import CONFIG_DIR

PLATFORM = 'switch'
DERIVED = re.compile(r'^r_(\d+)-(\d+)$')
STATUS_OF = {'open': 'open', 'closed': 'done', 'denied': 'declined'}
AEROFOIL_STATUS = {'open': 'open', 'done': 'closed', 'declined': 'denied'}


def _when(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ') if value is not None else None


class AeroFoilRequests:
    """`lantern.server.RequestStore` over AeroFoil's title requests. Call it inside an app context."""

    def __init__(self, path=None):
        path = path or os.path.join(CONFIG_DIR, 'lantern-request-ids.db')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute(
            'CREATE TABLE IF NOT EXISTS ids (id TEXT PRIMARY KEY, request_id INTEGER NOT NULL, '
            'user_id INTEGER NOT NULL, reason TEXT, UNIQUE (request_id, user_id))'
        )

    def close(self):
        with self._lock:
            self._db.close()

    # Ids.

    def _known(self):
        with self._lock:
            rows = self._db.execute('SELECT id, request_id, user_id, reason FROM ids').fetchall()
        return ({(r, u): (i, reason) for i, r, u, reason in rows}, {i: (r, u) for i, r, u, _ in rows})

    def _pair(self, request_id):
        """(AeroFoil request id, user id) behind a Lantern request id, or None."""
        _, by_id = self._known()
        if request_id in by_id:
            return by_id[request_id]
        match = DERIVED.match(request_id)
        return (int(match.group(1)), int(match.group(2))) if match else None

    # Reading.

    def _stored(self, row, user, ids):
        from lantern.server.title_requests import StoredRequest

        from app.lantern_native.accounts import lantern_name

        known = ids.get((row.id, user.id))
        reason = known[1] if known else None
        created = _when(row.created_at)
        return StoredRequest(
            id=known[0] if known else f'r_{row.id}-{user.id}',
            user=lantern_name(user.user),
            platform=PLATFORM,
            title_id=row.title_id,
            status=STATUS_OF.get((row.status or 'open').strip().lower(), 'open'),
            created=created,
            name=row.title_name or None,
            updated=created,
            reason=reason if STATUS_OF.get((row.status or '').strip().lower()) == 'declined' else None,
        )

    def _owner(self, name):
        from app.db import User
        from app.lantern_native.accounts import lantern_name

        return next((u for u in User.query.all() if lantern_name(u.user) == name), None)

    def requests(self, user=None):
        from app.db import TitleRequestUsers, TitleRequests, User

        ids, _ = self._known()
        owner = None
        if user is not None:
            owner = self._owner(user)
            if owner is None:
                return []
        query = (
            TitleRequestUsers.query.join(TitleRequests, TitleRequests.id == TitleRequestUsers.request_id)
            .join(User, User.id == TitleRequestUsers.user_id)
            .add_entity(TitleRequests)
            .add_entity(User)
        )
        if owner is not None:
            query = query.filter(TitleRequestUsers.user_id == owner.id)
        out = []
        for _, row, who in query.order_by(TitleRequests.created_at, TitleRequests.id).all():
            out.append(self._stored(row, who, ids))
        return out

    # Writing.

    def add(self, request):
        from lantern.server.http import ProblemError

        from app.title_requests import create_title_request

        owner = self._owner(request.user)
        if owner is None:
            raise LookupError(f'no AeroFoil user behind the account {request.user}')
        ok, message, row = create_title_request(owner.id, request.title_id, request.name)
        if not ok or row is None:
            raise ProblemError(409, 'conflict', message or 'The request could not be made.')
        if (row.status or 'open').strip().lower() != 'open':
            raise ProblemError(409, 'conflict', f'This title was already requested and is {row.status}.')
        with self._lock:
            self._db.execute(
                'INSERT OR REPLACE INTO ids (id, request_id, user_id, reason) VALUES (?, ?, ?, NULL)',
                (request.id, row.id, owner.id),
            )

    def set_status(self, request_id, status, *, reason=None, updated=None):
        from app.db import TitleRequests, User, db

        pair = self._pair(request_id)
        if pair is None:
            return None
        row = db.session.get(TitleRequests, pair[0])
        who = db.session.get(User, pair[1])
        if row is None or who is None:
            return None
        row.status = AEROFOIL_STATUS.get(status, 'open')
        db.session.commit()
        with self._lock:
            self._db.execute(
                'INSERT INTO ids (id, request_id, user_id, reason) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(request_id, user_id) DO UPDATE SET reason = excluded.reason',
                (request_id, row.id, who.id, reason if status == 'declined' else None),
            )
        ids, _ = self._known()
        return self._stored(row, who, ids)

    def remove(self, user, request_id):
        from app.db import TitleRequestUsers, TitleRequests, db

        owner = self._owner(user)
        pair = self._pair(request_id)
        if owner is None or pair is None or pair[1] != owner.id:
            return None
        link = TitleRequestUsers.query.filter_by(request_id=pair[0], user_id=owner.id).first()
        row = db.session.get(TitleRequests, pair[0])
        if link is None or row is None:
            return None
        ids, _ = self._known()
        gone = self._stored(row, owner, ids)
        db.session.delete(link)
        db.session.flush()
        if TitleRequestUsers.query.filter_by(request_id=row.id).count() == 0:
            db.session.delete(row)
        db.session.commit()
        with self._lock:
            self._db.execute('DELETE FROM ids WHERE request_id = ? AND user_id = ?', (pair[0], owner.id))
        return gone

    def forget(self, user):
        from app.db import TitleRequestUsers, TitleRequests, db

        owner = self._owner(user)
        if owner is None:
            return
        for link in TitleRequestUsers.query.filter_by(user_id=owner.id).all():
            row = db.session.get(TitleRequests, link.request_id)
            db.session.delete(link)
            db.session.flush()
            if row is not None and TitleRequestUsers.query.filter_by(request_id=row.id).count() == 0:
                db.session.delete(row)
        db.session.commit()
        with self._lock:
            self._db.execute('DELETE FROM ids WHERE user_id = ?', (owner.id,))
