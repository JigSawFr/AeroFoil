"""Storage and import helpers for Atmosphere cheat files.

Cheats are kept outside library paths so scans never mistake them for installable
content.  The layout mirrors Atmosphere: ``<title id>/<build id>.txt``.
"""

import hashlib
import io
import json
import os
import re
import zipfile

from app.constants import DATA_DIR


CHEATS_DIR = os.path.join(DATA_DIR, 'cheats')
TITLE_ID_RE = re.compile(r'^[0-9A-F]{16}$')
BUILD_ID_RE = re.compile(r'^[0-9A-F]{16,64}$')
MAX_CHEAT_FILE_BYTES = 2 * 1024 * 1024
MAX_SYNC_ARCHIVE_BYTES = 128 * 1024 * 1024


def normalize_title_id(value):
    value = str(value or '').strip().upper()
    return value if TITLE_ID_RE.fullmatch(value) else None


def normalize_build_id(value):
    value = str(value or '').strip().upper()
    return value if BUILD_ID_RE.fullmatch(value) else None


#: Called with the title id after a cheat file is written, noted or deleted (Lantern's events listen here).
listeners = []


def _changed(title_id):
    for listener in list(listeners):
        try:
            listener(title_id)
        except Exception:
            pass


def _cheat_path(title_id, build_id):
    return os.path.join(CHEATS_DIR, title_id, f'{build_id}.txt')


def _metadata_path():
    return os.path.join(CHEATS_DIR, 'metadata.json')


def _load_metadata():
    try:
        with open(_metadata_path(), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_metadata(metadata):
    os.makedirs(CHEATS_DIR, exist_ok=True)
    with open(_metadata_path(), 'w', encoding='utf-8') as handle:
        json.dump(metadata, handle, indent=2, sort_keys=True)


def _metadata_key(title_id, build_id):
    return f'{title_id}/{build_id}'


def _safe_cheat_text(data):
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError('Cheat data must be a text file.')
    if not data or len(data) > MAX_CHEAT_FILE_BYTES:
        raise ValueError('Cheat file must be between 1 byte and 2 MB.')
    try:
        data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Cheat file must be UTF-8 text.') from exc
    return bytes(data)


def save_cheat(title_id, build_id, data):
    title_id = normalize_title_id(title_id)
    build_id = normalize_build_id(build_id)
    if not title_id or not build_id:
        raise ValueError('A 16-character title ID and hexadecimal build ID are required.')
    data = _safe_cheat_text(data)
    path = _cheat_path(title_id, build_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(data)
    _changed(title_id)
    return {
        'title_id': title_id,
        'build_id': build_id,
        'size': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    }


def delete_cheat(title_id, build_id):
    title_id = normalize_title_id(title_id)
    build_id = normalize_build_id(build_id)
    if not title_id or not build_id:
        return False
    path = _cheat_path(title_id, build_id)
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    try:
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass
    metadata = _load_metadata()
    metadata.pop(_metadata_key(title_id, build_id), None)
    _save_metadata(metadata)
    _changed(title_id)
    return True


def list_cheats():
    result = []
    metadata = _load_metadata()
    if not os.path.isdir(CHEATS_DIR):
        return result
    for title_id in sorted(os.listdir(CHEATS_DIR)):
        if not normalize_title_id(title_id):
            continue
        title_dir = os.path.join(CHEATS_DIR, title_id)
        if not os.path.isdir(title_dir):
            continue
        for filename in sorted(os.listdir(title_dir)):
            build_id, extension = os.path.splitext(filename)
            if extension.lower() != '.txt' or not normalize_build_id(build_id):
                continue
            path = os.path.join(title_dir, filename)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            result.append({
                'title_id': title_id,
                'build_id': build_id.upper(),
                'size': int(stat.st_size),
                'updated_at': int(stat.st_mtime),
                'note': str(metadata.get(_metadata_key(title_id, build_id.upper()), '') or ''),
            })
    return result


def set_cheat_note(title_id, build_id, note):
    title_id = normalize_title_id(title_id)
    build_id = normalize_build_id(build_id)
    if not title_id or not build_id or not os.path.isfile(_cheat_path(title_id, build_id)):
        raise ValueError('Cheat file was not found.')
    note = str(note or '').strip()
    if len(note) > 1000:
        raise ValueError('Cheat note must be 1000 characters or less.')
    metadata = _load_metadata()
    key = _metadata_key(title_id, build_id)
    if note:
        metadata[key] = note
    else:
        metadata.pop(key, None)
    _save_metadata(metadata)
    _changed(title_id)
    return note


def cheat_state_token():
    entries = []
    for cheat in list_cheats():
        entries.append('%s:%s:%s:%s' % (
            cheat['title_id'], cheat['build_id'], cheat['size'], cheat['updated_at']))
    return hashlib.sha256('|'.join(entries).encode('utf-8')).hexdigest()


def read_cheat(title_id, build_id):
    title_id = normalize_title_id(title_id)
    build_id = normalize_build_id(build_id)
    if not title_id or not build_id:
        return None
    path = _cheat_path(title_id, build_id)
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except OSError:
        return None


def _archive_cheat_identity(member_name):
    parts = [part.upper() for part in member_name.replace('\\', '/').split('/') if part and part not in ('.', '..')]
    if not parts or not member_name.lower().endswith('.txt'):
        return None, None

    # Sources commonly use either <title>/<build>.txt or
    # <title>/cheats/<build>.txt (including the Atmosphere contents layout).
    # Also accept IDs wrapped in brackets or followed by a descriptive suffix.
    def _extract_identifier(value, validator, minimum, maximum):
        normalized = str(value or '').upper()
        direct = validator(normalized)
        if direct:
            return direct
        match = re.search(
            rf'(?<![0-9A-F])([0-9A-F]{{{minimum},{maximum}}})(?![0-9A-F])',
            normalized,
        )
        return validator(match.group(1)) if match else None

    build_id = _extract_identifier(
        os.path.splitext(parts[-1])[0], normalize_build_id, 16, 64)
    title_id = next(
        (
            _extract_identifier(part, normalize_title_id, 16, 16)
            for part in reversed(parts[:-1])
            if _extract_identifier(part, normalize_title_id, 16, 16)
        ),
        None,
    )
    return title_id, build_id


def import_zip(data):
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_SYNC_ARCHIVE_BYTES:
        raise ValueError('Cheat archive must be between 1 byte and 128 MB.')
    imported = 0
    skipped = 0
    ignored = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError('The cheat source did not return a valid ZIP archive.') from exc
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                ignored += 1
                continue
            if info.file_size > MAX_CHEAT_FILE_BYTES:
                skipped += 1
                continue
            title_id, build_id = _archive_cheat_identity(info.filename)
            if not title_id or not build_id:
                # Archives such as switch-cheats-db include title-description
                # text files alongside cheats; these are not errors.
                ignored += 1
                continue
            try:
                save_cheat(title_id, build_id, archive.read(info))
            except (OSError, ValueError):
                skipped += 1
                continue
            imported += 1
    return {'imported': imported, 'skipped': skipped, 'ignored': ignored}
