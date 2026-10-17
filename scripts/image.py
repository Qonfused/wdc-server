#!/usr/bin/env python3
"""Record and verify bootable image outputs and their build inputs."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


OUTPUTS = ('root.img', 'efi.img', 'rootfs.tar.xz')


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_inputs(project, package_lock, customization):
    paths = sorted((project / 'config').rglob('*.yml')) + [
        project / name for name in ('bootstrap.yml', 'prepare.yml', 'group_vars/all.yml',
                                   'build/image.yml', 'build/verify-nvidia.yml',
                                   'scripts/image.py', 'scripts/inputs.py',
                                   'scripts/prepare-packages.py', 'pyproject.toml',
                                   'uv.lock', '.python-version', 'locks/base.json',
                                   'locks/builds.yml', 'locks/collections.json',
                                   'locks/input-release.json')]
    if customization:
        paths.append(project / customization)
    return {'configuration_sha256': {str(path.relative_to(project)): checksum(path) for path in sorted(paths)},
            'package_lock_sha256': checksum(package_lock)}


def record(project, directory, package_lock, kind, user, profiles, customization):
    captured = json.loads((directory / 'source-inputs.json').read_text())
    if source_inputs(project, package_lock, customization) != captured:
        raise ValueError('Build inputs changed during image assembly; rebuild before releasing')
    lock = json.loads(package_lock.read_text())
    sources = captured['configuration_sha256']
    for name, source in [('packages.json', package_lock)] + [
            (name, project / 'locks' / name) for name in
            ('base.json', 'builds.yml', 'collections.json', 'input-release.json')]:
        (directory / name).write_bytes(source.read_bytes())
    revision = subprocess.run(['git', '-C', str(project), 'rev-parse', '--verify', 'HEAD'],
                              capture_output=True, text=True)
    manifest = {
        'schema': 1, 'kind': kind, 'user': user, 'profiles': profiles,
        'revision': revision.stdout.strip() if revision.returncode == 0 else None,
        'configuration_sha256': sources,
        'package_lock_sha256': checksum(package_lock),
        'kernel': lock['packages'][lock['kernel_package']]['version'],
        'files': {name: {'sha256': checksum(directory / name), 'size': (directory / name).stat().st_size}
                  for name in (*OUTPUTS, 'packages.json', 'base.json', 'builds.yml',
                               'collections.json', 'input-release.json', 'source-inputs.json')},
    }
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    sums = {name: entry['sha256'] for name, entry in manifest['files'].items()}
    sums['manifest.json'] = checksum(directory / 'manifest.json')
    (directory / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in sorted(sums.items())))
    return manifest


def verify(directory, kind=None):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('schema') != 1 or manifest.get('kind') not in ('release', 'test'):
        raise ValueError('Unsupported image manifest')
    if kind and manifest['kind'] != kind:
        raise ValueError('Expected an image of kind ' + kind)
    if not set(OUTPUTS).issubset(manifest.get('files', {})):
        raise ValueError('Image manifest lacks required boot artifacts')
    for name, entry in manifest['files'].items():
        if Path(name).name != name:
            raise ValueError('Invalid image output name: ' + name)
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != entry['size'] or checksum(path) != entry['sha256']:
            raise ValueError('Missing or changed image output: ' + name)
    if checksum(directory / 'packages.json') != manifest['package_lock_sha256']:
        raise ValueError('Image package lock disagrees with its manifest')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    make = commands.add_parser('record')
    make.add_argument('directory', type=Path)
    make.add_argument('--project', type=Path, required=True)
    make.add_argument('--lock', type=Path, required=True)
    make.add_argument('--kind', choices=['release', 'test'], required=True)
    make.add_argument('--user', required=True)
    make.add_argument('--profiles', type=json.loads, required=True)
    make.add_argument('--customization', default='')
    capture = commands.add_parser('snapshot')
    capture.add_argument('directory', type=Path)
    capture.add_argument('--project', type=Path, required=True)
    capture.add_argument('--lock', type=Path, required=True)
    capture.add_argument('--customization', default='')
    check = commands.add_parser('verify')
    check.add_argument('directory', type=Path)
    check.add_argument('--kind', choices=['release', 'test'])
    args = parser.parse_args()
    if args.operation == 'snapshot':
        captured = source_inputs(args.project.resolve(), args.lock, args.customization)
        with (args.directory / 'source-inputs.json').open('x') as output:
            output.write(json.dumps(captured, indent=2) + '\n')
        print(json.dumps({'source_files': len(captured['configuration_sha256'])}))
        return
    if args.operation == 'record':
        result = record(args.project.resolve(), args.directory, args.lock, args.kind, args.user,
                        args.profiles, args.customization)
    else:
        result = verify(args.directory, args.kind)
    print(json.dumps({'kind': result['kind'], 'files': len(result['files'])}))


if __name__ == '__main__':
    main()
