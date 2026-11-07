"""Lantern save backups, kept where AeroFoil keeps its own.

AeroFoil stores a user's saves under `data/saves/<user>/<TITLE ID>/[<type>/]<save id>.zip` with a `.json` beside
each; this is the SDK's `SaveStore` on that very layout, so a backup made by a Lantern client shows up in AeroFoil's
Saves page and the other way round, with nothing copied or migrated.

- The five kinds of Lantern (account, device, bcat, cache, other) are AeroFoil's save types. A backup with no kind
  goes in the historical untyped folder; what is already there is served as `other`.
- An id Lantern did not choose (AeroFoil's own saves have free names) is shown as `s_` and a hash of its place, the
  same every time.
- Lantern's own fields (slot, game version, profile, device, encryption, SHA-256) are kept in the `.json`, in a
  `lantern` object AeroFoil leaves alone. An encrypted backup is kept as sent in a `.enc` file, which AeroFoil's
  Saves page does not list: it cannot read it.
- Uploads under way live in `data/saves/.lantern-uploads/`, which AeroFoil does not read either.
- Erasing a Lantern account erases only what Lantern wrote: AeroFoil's own saves are not Lantern's to delete.
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from werkzeug.utils import secure_filename

from app.constants import DATA_DIR

SAVE_SYNC_DIR = os.path.join(DATA_DIR, 'saves')
KINDS = ('account', 'device', 'bcat', 'cache', 'other')
UPLOADS = '.lantern-uploads'
PLATFORM = 'switch'
TITLE_ID = re.compile(r'^[0-9A-F]{16}$')
LANTERN_ID = re.compile(r'^s_[A-Za-z0-9_-]{1,64}$')


def _time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _timestamp(text):
    try:
        return int(datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return int(time.time())


class AeroFoilSaveStore:
    """`lantern.server.SaveStore` over AeroFoil's saves folder."""

    def __init__(self, folder=SAVE_SYNC_DIR, refresh=None):
        self.folder = folder
        #: Called as refresh(aerofoil_user, title_id, save_type) after a change, so AeroFoil rebuilds its
        #: "latest" copy of a title (set by app.py; see set_save_refresher).
        self.refresh = refresh
        self._lock = threading.RLock()
        self._sha = {}

    # Where things are.

    def _owner(self, name):
        from app.db import User
        from app.lantern_native.accounts import lantern_name

        for user in User.query.all():
            if lantern_name(user.user) == name:
                return user
        return None

    def _user_dir(self, user):
        key = secure_filename(str(user.user or '').strip())
        if not key and user.id is not None:
            key = secure_filename(str(user.id))
        return os.path.join(self.folder, key or 'unknown')

    def _uploads(self):
        return os.path.join(self.folder, UPLOADS)

    @staticmethod
    def _id(title_id, folder, stem):
        if LANTERN_ID.match(stem):
            return stem
        return 's_' + hashlib.sha1(f'{title_id}/{folder}/{stem}'.encode()).hexdigest()[:16]

    def _digest(self, path):
        info = os.stat(path)
        key = (path, info.st_mtime_ns, info.st_size)
        known = self._sha.get(key)
        if known is None:
            digest = hashlib.sha256()
            with open(path, 'rb') as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
            known = self._sha[key] = digest.hexdigest()
        return known

    def _meta(self, path):
        try:
            with open(path, encoding='utf-8') as handle:
                doc = json.load(handle)
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _entries(self, user):
        """Every backup of an AeroFoil user: (Save, archive path, metadata path or None, folder name)."""
        from lantern._generated.models import Save
        from lantern._generated.models import Encryption

        out = []
        root = self._user_dir(user)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return out
        for name in names:
            path = os.path.join(root, name)
            if os.path.isfile(path) and name.lower().endswith('.zip') and TITLE_ID.match(name[:-4].upper()):
                # AeroFoil's oldest layout: one archive per title, straight in the user's folder
                title_id = name[:-4].upper()
                ts = int(os.path.getmtime(path))
                out.append((
                    Save(
                        platform=PLATFORM, title_id=title_id, save_id=self._id(title_id, '', 'legacy'),
                        created=_time(ts), size=os.path.getsize(path), sha256=self._digest(path),
                        slot='default', kind='other',
                    ),
                    path, None, '', False,
                ))
                continue
            if not TITLE_ID.match(name) or not os.path.isdir(path):
                continue
            for folder in ('', *KINDS):
                where = os.path.join(path, folder) if folder else path
                try:
                    files = sorted(os.listdir(where))
                except OSError:
                    continue
                for file in files:
                    stem, dot, extension = file.rpartition('.')
                    if not dot or extension.lower() not in ('zip', 'enc') or file == '_latest.zip':
                        continue
                    archive = os.path.join(where, file)
                    if not os.path.isfile(archive):
                        continue
                    meta_path = os.path.join(where, stem + '.json')
                    meta = self._meta(meta_path)
                    ours = meta.get('lantern') if isinstance(meta.get('lantern'), dict) else {}
                    ts = meta.get('created_ts')
                    if not isinstance(ts, int) or ts <= 0:
                        ts = int(os.path.getmtime(archive))
                    encryption = ours.get('encryption')
                    kind = ours.get('kind') or (folder if folder else 'other')
                    save = Save(
                        platform=PLATFORM,
                        title_id=name,
                        save_id=self._id(name, folder, stem),
                        created=_time(ts),
                        size=os.path.getsize(archive),
                        sha256=self._digest(archive),
                        slot=ours.get('slot') or 'default',
                        kind=kind if kind in KINDS else 'other',
                        game_version_code=ours.get('game_version_code'),
                        profile=ours.get('profile'),
                        note=(str(meta.get('note') or '').strip() or None),
                        device=ours.get('device'),
                        encryption=Encryption.from_json(encryption) if isinstance(encryption, dict) else None,
                    )
                    out.append((save, archive, meta_path if os.path.isfile(meta_path) else None, folder, bool(ours)))
        out.sort(key=lambda e: (e[0].created, e[0].save_id))
        return out

    # SaveStore.

    def saves(self, user):
        owner = self._owner(user)
        if owner is None:
            return []
        with self._lock:
            return [entry[0] for entry in self._entries(owner)]

    def archive(self, user, save_id):
        from lantern.server.files import FileRef

        owner = self._owner(user)
        if owner is None:
            return None
        with self._lock:
            for save, path, _, _, _ in self._entries(owner):
                if save.save_id == save_id:
                    type_ = 'application/octet-stream' if save.encryption is not None else 'application/zip'
                    return FileRef(path, save.size, save.sha256, type_)
        return None

    def _place(self, owner, save):
        from lantern.server.http import bad_request

        title_id = str(save.title_id or '').upper()
        if not TITLE_ID.match(title_id):
            raise bad_request('A Switch title id is 16 hexadecimal digits.')
        folder = save.kind if save.kind in KINDS else ''
        where = os.path.join(self._user_dir(owner), title_id, folder) if folder else os.path.join(self._user_dir(owner), title_id)
        os.makedirs(where, exist_ok=True)
        return title_id, folder, where

    def _write_meta(self, where, save, ts):
        ours = {
            k: v
            for k, v in {
                'slot': save.slot,
                'kind': save.kind,
                'game_version_code': save.game_version_code,
                'profile': save.profile,
                'device': save.device,
                'sha256': save.sha256,
                'encryption': save.encryption.to_json() if save.encryption is not None else None,
            }.items()
            if v is not None
        }
        doc = {'note': save.note or '', 'created_ts': ts, 'created_at': save.created, 'lantern': ours}
        tmp = os.path.join(where, f'.{save.save_id}.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(doc, handle)
        os.replace(tmp, os.path.join(where, save.save_id + '.json'))

    def _commit(self, owner, save, source, move):
        title_id, folder, where = self._place(owner, save)
        extension = 'enc' if save.encryption is not None else 'zip'
        target = os.path.join(where, f'{save.save_id}.{extension}')
        if move:
            shutil.move(source, target)
        else:
            tmp = os.path.join(where, f'.{save.save_id}.part')
            with open(tmp, 'wb') as handle:
                handle.write(source)
            os.replace(tmp, target)
        self._write_meta(where, save, _timestamp(save.created))
        self._changed(owner, title_id, folder)

    def _changed(self, owner, title_id, folder):
        if self.refresh is not None:
            try:
                self.refresh(owner, title_id, folder or 'unknown')
            except Exception:
                pass

    def keep(self, user, save, data):
        owner = self._owner(user)
        if owner is None:
            raise LookupError(f'no AeroFoil user behind the account {user}')
        with self._lock:
            self._commit(owner, save, bytes(data), move=False)

    def drop(self, user, save_id):
        owner = self._owner(user)
        if owner is None:
            return False
        with self._lock:
            for save, path, meta, folder, _ in self._entries(owner):
                if save.save_id == save_id:
                    os.remove(path)
                    if meta:
                        os.remove(meta)
                    self._changed(owner, save.title_id, folder)
                    return True
        return False

    # Uploads.

    def _session_path(self, upload_id):
        return os.path.join(self._uploads(), upload_id + '.json')

    def _part_path(self, upload_id):
        return os.path.join(self._uploads(), upload_id + '.part')

    def _load(self, upload_id):
        from lantern._generated.models import Save
        from lantern.server.saves import UploadSession

        try:
            with open(self._session_path(upload_id), encoding='utf-8') as handle:
                doc = json.load(handle)
            offset = os.path.getsize(self._part_path(upload_id))
            return UploadSession(
                id=doc['id'], user=doc['user'], save=Save.from_json(doc['save']), expires=doc['expires'], offset=offset
            )
        except (OSError, ValueError, KeyError):
            return None

    def open_upload(self, session):
        with self._lock:
            os.makedirs(self._uploads(), exist_ok=True)
            with open(self._part_path(session.id), 'wb'):
                pass
            doc = {'id': session.id, 'user': session.user, 'save': session.save.to_json(), 'expires': session.expires}
            with open(self._session_path(session.id), 'w', encoding='utf-8') as handle:
                json.dump(doc, handle)

    def upload(self, upload_id):
        with self._lock:
            return self._load(upload_id)

    def uploads(self):
        with self._lock:
            try:
                names = [n[:-5] for n in os.listdir(self._uploads()) if n.endswith('.json')]
            except OSError:
                return []
            return [s for s in (self._load(n) for n in sorted(names)) if s is not None]

    def append(self, upload_id, data):
        with self._lock:
            with open(self._part_path(upload_id), 'ab') as handle:
                handle.write(data)
            return os.path.getsize(self._part_path(upload_id))

    def upload_digest(self, upload_id):
        with self._lock:
            digest = hashlib.sha256()
            with open(self._part_path(upload_id), 'rb') as handle:
                head = handle.read(16)
                digest.update(head)
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
            return digest.hexdigest(), head

    def keep_upload(self, upload_id, save):
        with self._lock:
            session = self._load(upload_id)
            owner = self._owner(session.user) if session is not None else None
            if session is None or owner is None:
                raise LookupError('the upload or its account is gone')
            self._commit(owner, save, self._part_path(upload_id), move=True)
            os.remove(self._session_path(upload_id))

    def abandon(self, upload_id):
        with self._lock:
            for path in (self._part_path(upload_id), self._session_path(upload_id)):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def forget(self, user):
        owner = self._owner(user)
        with self._lock:
            for session in self.uploads():
                if session.user == user:
                    self.abandon(session.id)
            if owner is None:
                return
            for save, path, meta, folder, ours in self._entries(owner):
                if ours:  # written by Lantern
                    os.remove(path)
                    if meta:
                        os.remove(meta)
                    self._changed(owner, save.title_id, folder)
