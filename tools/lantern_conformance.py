"""Runs the Lantern conformance suite against a throwaway AeroFoil filled with fictional packages.

    python tools/lantern_conformance.py --runner path/to/lantern-sdk/spec/conformance/run.py [--admin-writes]

The runner is the suite of the Lantern specification (`lantern/conformance/run.py`, also in the SDK's `spec/`
submodule). The script copies this checkout to a temporary folder, makes a database with four fictional packages
and a paired device, starts AeroFoil there with Lantern on, waits for the packages to be hashed, runs the suite and
stops. Nothing of your own configuration or library is touched. Exit status: the suite's.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = 'et_conformance-0123456789abcdef0123456789abcdef'

SETUP = r'''
import os, sys
sys.path.insert(0, '.')
os.environ['AEROFOIL_SECRET_KEY'] = 'conformance'
from app.app import app, init_db
from app.db import Apps, Files, Libraries, Titles, User, db
from lantern.server import SqliteAccounts
from lantern.server.accounts import SCOPES
from lantern.signatures import DeviceKey

init_db(app)
PACKAGES = [
    ("Frost Garden [0100ABCD00010000][v0].nsp", b"frost" * 400, '0100ABCD00010000', '0', 'BASE', 'nsp'),
    ("Frost Garden [0100ABCD00010800][v65536].nsp", b"update" * 300, '0100ABCD00010800', '65536', 'UPDATE', 'nsp'),
    ("Frost Garden - Winter [0100ABCD00011001][v0].nsp", b"winter" * 200, '0100ABCD00011001', '0', 'DLC', 'nsp'),
    ("Moss Walk [0100ABCD00020000][v0].xci", b"moss" * 500, '0100ABCD00020000', '0', 'BASE', 'xci'),
]
with app.app_context():
    library = Libraries(path=os.path.abspath('games')); db.session.add(library); db.session.flush()
    for name, data, app_id, version, kind, ext in PACKAGES:
        path = os.path.abspath(os.path.join('games', name))
        with open(path, 'wb') as handle:
            handle.write(data)
        low = int(app_id, 16) & 0xFFF
        title_id = app_id if low == 0 else (app_id[:13] + '000' if low == 0x800 else f'{(int(app_id, 16) & ~0xFFF) - 0x1000:016X}')
        title = Titles.query.filter_by(title_id=title_id).first() or Titles(title_id=title_id)
        db.session.add(title); db.session.flush()
        file = Files(library_id=library.id, filepath=path, filename=name, extension=ext, size=len(data), identified=True)
        entry = Apps(title_id=title.id, app_id=app_id, app_version=version, app_type=kind, owned=True)
        entry.files.append(file)
        db.session.add_all([file, entry])
    db.session.add(User(user='mira', password='x', admin_access=True, shop_access=True, backup_access=True, cheat_access=True, frozen=False))
    db.session.commit()
key = DeviceKey.generate()
with open('device.key', 'w') as handle:
    handle.write(key.seed_b64() + '\n')
with SqliteAccounts('app/config/lantern-accounts.db') as accounts:
    accounts.create_user('mira', SCOPES)
    accounts.add_token(sys.argv[1], 'mira', "Mira's console", SCOPES, key=key.public)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runner', required=True, help="the suite's run.py")
    parser.add_argument('--port', type=int, default=18470)
    parser.add_argument('--admin-writes', action='store_true', help='also run the checks that make and delete accounts')
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix='aerofoil-conformance-')
    try:
        shutil.copytree(ROOT, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git', 'config', 'data', '__pycache__'))
        for folder in ('app/config', 'app/data', 'games'):
            os.makedirs(os.path.join(work, folder), exist_ok=True)
        with open(os.path.join(work, 'app/config/settings.yaml'), 'w', encoding='utf-8') as handle:
            handle.write('shop:\n  lantern_enabled: true\n  public: true\n')
        subprocess.run([sys.executable, '-c', SETUP, TOKEN], cwd=work, check=True, stdout=subprocess.DEVNULL)
        env = dict(os.environ, AEROFOIL_PORT=str(args.port), AEROFOIL_SECRET_KEY='conformance')
        server = subprocess.Popen([sys.executable, 'app/app.py'], cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            url = f'http://127.0.0.1:{args.port}/'
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(url + 'api/lantern/v1/apps', timeout=3) as answer:
                        if b'"total":4' in answer.read().replace(b' ', b''):
                            break
                except OSError:
                    pass
                time.sleep(2)
            else:
                print('AeroFoil did not list the four packages in time.', file=sys.stderr)
                return 2
            command = [sys.executable, args.runner, url, '--token', TOKEN, '--key', os.path.join(work, 'device.key')]
            if args.admin_writes:
                command.append('--admin-writes')
            return subprocess.run(command, check=False).returncode
        finally:
            server.terminate()
            server.wait(timeout=15)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
