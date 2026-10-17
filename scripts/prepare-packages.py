#!/usr/bin/env python3
"""Select SHA-256-pinned archives and index them with native XBPS tools."""

import argparse
import hashlib
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matches(path, digest):
    try:
        return path.is_file() and checksum(path) == digest
    except OSError:
        return False


def select_archives(lock, destination, caches, downloads, repository_url=None):
    expected = {
        f"{entry['version']}.{entry['architecture']}.xbps": entry
        for entry in lock["packages"].values()
    }
    available = {}
    for directory in caches:
        for path in directory.rglob("*.xbps"):
            available.setdefault(path.name, []).append(path)

    # Every input must match its pin before the repository is created or changed.
    sources = {}
    for filename, entry in expected.items():
        existing = destination / filename
        if matches(existing, entry["sha256"]):
            sources[filename] = existing
            continue
        for candidate in available.get(filename, []):
            if matches(candidate, entry["sha256"]):
                sources[filename] = candidate
                break
        else:
            urls = ([repository_url.rstrip('/') + '/' + filename] if repository_url else [])
            if entry.get('url'):
                urls.append(entry['url'])
            downloaded = downloads / filename
            failures = []
            for url in dict.fromkeys(urls):
                try:
                    with urllib.request.urlopen(url, timeout=60) as response, downloaded.open("wb") as output:
                        while data := response.read(1024 * 1024):
                            output.write(data)
                    if checksum(downloaded) != entry["sha256"]:
                        raise ValueError("Downloaded artifact differs from the lock")
                except (OSError, ValueError) as error:
                    downloaded.unlink(missing_ok=True)
                    failures.append(f"{url}: {error}")
                    continue
                sources[filename] = downloaded
                break
            else:
                detail = "; ".join(failures) or "no download source is declared"
                raise SystemExit(f"Cannot obtain locked artifact {filename}: {detail}")
    return sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--cache", type=Path, action="append", default=[])
    parser.add_argument("--repository-url", help="Archive mirror to try before the locked upstream URL")
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text())
    expected = {f"{entry['version']}.{entry['architecture']}.xbps": entry
                for entry in lock['packages'].values()}
    with tempfile.TemporaryDirectory(prefix="wdc-package-inputs-") as directory:
        sources = select_archives(lock, args.destination, args.cache, Path(directory), args.repository_url)
        extras = {p.name for p in args.destination.glob("*.xbps")} - sources.keys()

        args.destination.mkdir(parents=True, exist_ok=True)
        changed = False
        for filename, source in sources.items():
            destination = args.destination / filename
            if source == destination:
                continue
            with tempfile.TemporaryDirectory(prefix=".wdc-archive-", dir=args.destination) as temporary:
                temporary_path = Path(temporary) / filename
                subprocess.run(["cp", "--reflink=auto", str(source), str(temporary_path)], check=True)
                if not matches(temporary_path, expected[filename]["sha256"]):
                    raise SystemExit(f"Artifact changed while copying: {filename}")
                temporary_path.replace(destination)
            changed = True
        for filename in extras:
            (args.destination / filename).unlink()
            changed = True

    manifest = args.destination / "lock.json"
    contents = args.lock.read_bytes()
    index = args.destination / f"{lock['architecture']}-repodata"
    try:
        previous_index = checksum(index) if index.is_file() else None
    except OSError:
        previous_index = None
    # Recreate XBPS metadata from verified packages. An old index may be corrupt
    # even when every archive passes its checksum.
    index.unlink(missing_ok=True)
    subprocess.run(["xbps-rindex", "-a", *map(str, sorted(args.destination.glob("*.xbps")))],
                   check=True, stdout=subprocess.DEVNULL)
    changed = changed or checksum(index) != previous_index
    if not manifest.exists() or manifest.read_bytes() != contents:
        manifest.write_bytes(contents)
        changed = True
    print(json.dumps({"changed": changed, "packages": len(sources)}))


if __name__ == "__main__":
    main()
