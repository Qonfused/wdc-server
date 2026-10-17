#!/usr/bin/env python3
"""Preserve and verify the exact installation inputs for a release."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


MAX_PART_BYTES = 1_500_000_000
RELEASE_ASSET_LIMIT = 2 * 1024 ** 3
INPUT_LOCKS = ('packages.json', 'base.json', 'collections.json', 'builds.yml')


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def release_tag(project):
    digest = hashlib.sha256()
    for name in INPUT_LOCKS:
        digest.update(name.encode() + b'\0')
        digest.update((project / 'locks' / name).read_bytes())
    return 'inputs-' + digest.hexdigest()[:12]


def expected_files(project, package_lock):
    files = {'locks/packages.json': (package_lock, checksum(package_lock))}
    for name in ('base.json', 'collections.json', 'builds.yml'):
        source = project / 'locks' / name
        files['locks/' + name] = (source, checksum(source))
    base = json.loads((project / 'locks/base.json').read_text())
    files['base/rootfs.tar.xz'] = (project / '.work/base/rootfs.tar.xz', base['sha256'])
    for entry in json.loads((project / 'locks/collections.json').read_text())['archives']:
        files['collection-archives/' + entry['file']] = (
            project / '.work/collection-archives' / entry['file'], entry['sha256'])
    for entry in json.loads(package_lock.read_text())['packages'].values():
        name = entry['version'] + '.' + entry['architecture'] + '.xbps'
        files['packages/' + name] = (project / '.work/packages' / name, entry['sha256'])
    return files


def create(project, package_lock, packages, destination):
    if destination.exists():
        raise ValueError('Input releases cannot be overwritten: ' + str(destination))
    files = expected_files(project, package_lock)
    if packages:
        files = {name: (packages / Path(name).name if name.startswith('packages/') else source, digest)
                 for name, (source, digest) in files.items()}
    for name, (source, digest) in files.items():
        if not source.is_file() or checksum(source) != digest:
            raise ValueError('Missing or changed release input: ' + name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wdc-inputs-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'release'
        staging.mkdir()
        for name, (source, _) in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['cp', '--reflink=auto', str(source), str(target)], check=True)
        manifest = {'schema': 1, 'files': {name: digest for name, (_, digest) in sorted(files.items())}}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        verify(staging)
        staging.rename(destination)
    return manifest


def verify(directory, project=None, package_lock=None):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('schema') != 1 or not manifest.get('files'):
        raise ValueError('Unsupported or empty input manifest')
    for name, digest in manifest['files'].items():
        path = directory / name
        if Path(name).is_absolute() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError('Input path escapes the release: ' + name)
        if path.is_symlink() or not path.is_file() or checksum(path) != digest:
            raise ValueError('Missing or changed release input: ' + name)
    present = {str(path.relative_to(directory)) for path in directory.rglob('*')
               if path.is_file() or path.is_symlink()}
    if present != manifest['files'].keys() | {'manifest.json'}:
        raise ValueError('Input release contains unlisted or missing files')
    expected = expected_files(directory, directory / 'locks/packages.json')
    if manifest['files'].keys() != expected.keys():
        raise ValueError('Input manifest must contain exactly the locked installation inputs')
    for name, (_, digest) in expected.items():
        if manifest['files'][name] != digest:
            raise ValueError('Input manifest disagrees with its lock: ' + name)
    if project:
        for name, (_, digest) in expected_files(project, package_lock or project / 'locks/packages.json').items():
            if manifest['files'].get(name) != digest:
                raise ValueError('Input release disagrees with the checkout: ' + name)
    return manifest


def pack(directory, destination, source, project):
    verify(directory, project)
    if destination.exists():
        raise ValueError('Input release parts cannot be overwritten: ' + str(destination))
    paths = sorted(path for path in directory.rglob('*') if path.is_file())
    groups = []
    group_size = 0
    for path in paths:
        size = path.stat().st_size
        if size > MAX_PART_BYTES:
            raise ValueError('Input exceeds the release asset size: ' + str(path))
        if not groups or group_size + size > MAX_PART_BYTES:
            groups.append([])
            group_size = 0
        groups[-1].append(path)
        group_size += size

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wdc-release-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'parts'
        staging.mkdir()
        parts = []
        for index, group in enumerate(groups, 1):
            name = f'inputs-{index:03d}.tar'
            archive = staging / name
            with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as output:
                for path in group:
                    member = tarfile.TarInfo(path.relative_to(directory).as_posix())
                    member.size = path.stat().st_size
                    member.mode = 0o644
                    with path.open('rb') as stream:
                        output.addfile(member, stream)
            if archive.stat().st_size >= RELEASE_ASSET_LIMIT:
                raise ValueError('Release asset exceeds the GitHub size limit: ' + name)
            parts.append({'file': name, 'size': archive.stat().st_size, 'sha256': checksum(archive)})
        release = {
            'schema': 1,
            'lock_sha256': {name: checksum(project / 'locks' / name) for name in INPUT_LOCKS},
            'source': source.rstrip('/') + '/',
            'parts': parts,
        }
        (staging / 'input-release.json').write_text(json.dumps(release, indent=2) + '\n')
        staging.rename(destination)
    return release


def check_release_lock(release_lock, project):
    release = json.loads(release_lock.read_text())
    if release.get('schema') != 1 or not isinstance(release.get('parts'), list) or not release['parts']:
        raise ValueError('Unsupported input release lock')
    if release.get('lock_sha256') != {name: checksum(project / 'locks' / name) for name in INPUT_LOCKS}:
        raise ValueError('Input release does not match the checkout locks')
    if not isinstance(release.get('source'), str) or not release['source']:
        raise ValueError('Input release has no source')
    names = set()
    for part in release['parts']:
        if not isinstance(part, dict):
            raise ValueError('Invalid release asset entry')
        name = part.get('file')
        if not isinstance(name, str) or not re.fullmatch(r'inputs-\d{3}\.tar', name) or name in names:
            raise ValueError('Invalid or repeated release asset name')
        if type(part.get('size')) is not int or not 0 < part['size'] < RELEASE_ASSET_LIMIT:
            raise ValueError('Invalid release asset size: ' + name)
        if not isinstance(part.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', part['sha256']):
            raise ValueError('Invalid release asset checksum: ' + name)
        names.add(name)
    return release


def restore(release_lock, destination, project, source=None):
    release = check_release_lock(release_lock, project)
    if destination.exists():
        try:
            verify(destination, project)
            return release
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    destination.parent.mkdir(parents=True, exist_ok=True)
    downloads = destination.with_name(destination.name + '.parts')
    downloads.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wdc-inputs-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'inputs'
        staging.mkdir()
        seen = set()
        for part in release['parts']:
            name = part['file']
            archive = downloads / name
            if not archive.is_file() or archive.stat().st_size != part['size'] or checksum(archive) != part['sha256']:
                url = (source or release['source']).rstrip('/') + '/' + name
                with urllib.request.urlopen(url, timeout=120) as response, archive.open('wb') as output:
                    shutil.copyfileobj(response, output)
                if archive.stat().st_size != part['size'] or checksum(archive) != part['sha256']:
                    archive.unlink(missing_ok=True)
                    raise ValueError('Downloaded release asset differs from the lock: ' + name)
            with tarfile.open(archive, 'r:') as stream:
                for member in stream:
                    path = Path(member.name)
                    if (not member.isfile() or path.is_absolute() or '..' in path.parts
                            or path.as_posix() != member.name or path in seen):
                        raise ValueError('Invalid or repeated input release member: ' + member.name)
                    seen.add(path)
                    target = staging / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(member) as input_stream, target.open('wb') as output:
                        shutil.copyfileobj(input_stream, output)
        verify(staging, project)
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest='operation', required=True)
    tag = subcommands.add_parser('tag')
    tag.add_argument('--project', type=Path, default=Path(__file__).resolve().parent.parent)
    create_parser = subcommands.add_parser('create')
    create_parser.add_argument('destination', type=Path)
    create_parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parent.parent)
    create_parser.add_argument('--lock', type=Path)
    create_parser.add_argument('--packages', type=Path)
    check = subcommands.add_parser('verify')
    check.add_argument('directory', type=Path)
    check.add_argument('--project', type=Path, help='Compare with this checkout as well as the embedded locks')
    check.add_argument('--lock', type=Path, help='Candidate package lock for the selected checkout')
    archive_parser = subcommands.add_parser('pack')
    archive_parser.add_argument('directory', type=Path)
    archive_parser.add_argument('destination', type=Path)
    archive_parser.add_argument('--source', required=True, help='URL containing the release assets')
    archive_parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parent.parent)
    fetch = subcommands.add_parser('restore')
    fetch.add_argument('release_lock', type=Path)
    fetch.add_argument('destination', type=Path)
    fetch.add_argument('--project', type=Path, default=Path(__file__).resolve().parent.parent)
    fetch.add_argument('--source', help='Alternate release URL for testing or a private mirror')
    check_release = subcommands.add_parser('check')
    check_release.add_argument('release_lock', type=Path)
    check_release.add_argument('--project', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    if args.operation == 'tag':
        print(release_tag(args.project))
        return
    if args.operation == 'create':
        result = create(args.project, args.lock or args.project / 'locks/packages.json',
                        args.packages, args.destination)
    elif args.operation == 'verify':
        if args.lock and not args.project:
            parser.error('--lock requires --project')
        result = verify(args.directory, args.project, args.lock)
    elif args.operation == 'pack':
        result = pack(args.directory, args.destination, args.source, args.project)
    elif args.operation == 'check':
        result = check_release_lock(args.release_lock, args.project)
    else:
        result = restore(args.release_lock, args.destination, args.project, args.source)
    print(json.dumps({'files': len(result['files'])} if 'files' in result else
                     {'parts': len(result['parts'])}))


if __name__ == '__main__':
    main()
