#!/usr/bin/env python3
"""Reject machine identities and disposable-test access in a release rootfs."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib


PROJECT = Path(__file__).resolve().parent.parent


def inspect(directory):
    verified = subprocess.run([sys.executable, str(PROJECT / 'scripts/image.py'), 'verify',
                               str(directory), '--kind', 'release'],
                              capture_output=True, text=True)
    if verified.returncode:
        raise ValueError(verified.stderr.strip())
    manifest = json.loads((directory / 'manifest.json').read_text())
    user = manifest['user']
    forbidden = {'etc/machine-id', 'var/lib/dbus/machine-id', 'etc/sudoers.d/wdc-qemu',
                 f'home/{user}/.ssh/authorized_keys', '.dockerenv', 'run/.containerenv',
                 'root/.config/rustdesk/RustDesk.toml', f'home/{user}/.config/rustdesk/RustDesk.toml'}
    prefixes = ('etc/ssh/ssh_host_', 'var/lib/tailscale/')
    preferences = set()
    accounts = {}
    with tarfile.open(directory / 'rootfs.tar.xz', 'r|xz') as archive:
        for entry in archive:
            name = entry.name.removeprefix('./')
            if name in forbidden or name.startswith(prefixes):
                raise ValueError('Release contains machine or test state: ' + name)
            if name.endswith('/.config/rustdesk/RustDesk2.toml') and entry.isfile():
                settings = tomllib.loads(archive.extractfile(entry).read().decode())
                if settings.keys() - {'options'}:
                    raise ValueError('Release contains private RustDesk connection state: ' + name)
                if settings.get('options'):
                    preferences.add(name)
            if name == 'etc/shadow':
                accounts = {row.split(':')[0]: row.split(':')[1]
                            for row in archive.extractfile(entry).read().decode().splitlines()}
            if name == 'etc/sv/agetty-tty1/conf' and entry.isfile():
                if b'--autologin' in archive.extractfile(entry).read():
                    raise ValueError('Release enables console autologin')
    for account in ('root', user):
        if accounts.get(account) != '!':
            raise ValueError('Release account is not locked: ' + account)
    if 'rustdesk' in manifest['profiles'] and 'base' in manifest['profiles']:
        required = {'root/.config/rustdesk/RustDesk2.toml',
                    f'home/{user}/.config/rustdesk/RustDesk2.toml'}
        if preferences != required:
            raise ValueError('Release lost its declared RustDesk preferences')
    return {'kind': 'release', 'locked_accounts': sorted(accounts.keys() & {'root', user}),
            'machine_identities': 'absent', 'test_access': 'absent'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.directory)))


if __name__ == '__main__':
    main()
