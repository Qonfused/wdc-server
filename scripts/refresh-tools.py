#!/usr/bin/env python3
"""Refresh candidate build-tool pins using xbps-src and native XBPS transactions."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid

import yaml

from xbps import repository_index, transaction_packages


def obtain(url, digest, destination, caches):
    for path in [destination, *caches]:
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            if path != destination:
                shutil.copyfile(path, destination)
            return
    with urllib.request.urlopen(url, timeout=60) as source, destination.open("wb") as output:
        shutil.copyfileobj(source, output)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        destination.unlink()
        raise RuntimeError(f"Source checksum mismatch: {url}")


def refresh(project, work, declarations, name, recipes):
    toolchain = declarations["toolchains"][name]
    if not re.search(r"@sha256:[a-f0-9]{64}$", toolchain["image"]):
        raise RuntimeError(f"Build environment must be pinned by digest: {name}")
    directory = work / name
    directory.mkdir(parents=True)
    container = "wdc-server-tools-" + uuid.uuid4().hex[:12]
    logs = directory / "native.log"

    def native(*arguments, cwd=None, check=True):
        command = ["docker", "exec"]
        if cwd:
            command += ["--workdir", cwd]
        command += [container, *arguments]
        result = subprocess.run(command, capture_output=True, text=True)
        with logs.open("a") as output:
            output.write("$ " + " ".join(command) + "\n" + result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"Native tool refresh failed: {' '.join(arguments)}\n{result.stderr}")
        return result

    def xbps(program, *arguments, root="/output/root", repository=None):
        return native(program, "--rootdir", root, "--ignore-conf-repos",
                      "--repository", repository or toolchain["repository"], *arguments).stdout

    try:
        subprocess.run(["docker", "run", "--detach", "--name", container,
                        "--platform", toolchain["platform"], "--runtime", "runc",
                        "--mount", f"type=bind,src={directory},dst=/output",
                        toolchain["image"], "sleep", "infinity"],
                       check=True, capture_output=True, text=True)
        # Copy only the image's package database. Transactions are resolved in
        # separate roots so conflicting per-component compilers can coexist in
        # the archive repository without being installed together.
        native("mkdir", "-p", "/output/root/var/db")
        native("cp", "-a", "/var/db/xbps", "/output/root/var/db/")
        xbps("xbps-install", "--sync")
        metadata, indexes = {}, []
        for path in sorted((directory / "root/var/db/xbps").rglob("*-repodata")):
            for entry in repository_index(container, "/output/" + str(path.relative_to(directory))).values():
                metadata.setdefault(entry["pkgver"], []).append(entry)
            indexes.append({"file": str(path.relative_to(work)),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        if not indexes:
            raise RuntimeError("XBPS did not retain repository indexes")

        groups = {"bootstrap": toolchain["bootstrap_roots"]}
        sources = {}
        for package in recipes:
            recipe = declarations["packages"][package]
            source = declarations["sources"][recipe["source"]]
            archive = directory / (package + "-source.tar.gz")
            obtain(source["archive_url"] + "/" + source["commit"], source["sha256"], archive,
                   list((project / ".work").glob("**/void-packages-" + source["commit"] + ".tar.gz")))
            tree = "/output/source-" + package
            native("mkdir", "-p", tree)
            native("bsdtar", "-xf", "/output/" + archive.name, "-C", tree, "--strip-components", "1")
            if "overlay" in recipe:
                overlay = declarations["sources"][recipe["overlay"]]
                path = directory / (package + "-overlay.tar.gz")
                obtain(overlay["archive_url"] + "/" + overlay["commit"], overlay["sha256"], path,
                       [project / ".work/package-builds" / package / "overlay.tar.gz"])
                native("bsdtar", "-xf", "/output/" + path.name, "-C", tree,
                       "--strip-components", "1", "*/srcpkgs/" + package + "/*")
            if "pull_request" in recipe:
                patch = recipe["pull_request"]
                path = directory / (package + ".diff")
                obtain(patch["url"], patch["sha256"], path,
                       [project / ".work/package-builds" / package / "pull-request.diff"])
                native("patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i",
                       "/output/" + path.name, cwd=tree)
            configuration = directory / (package + "-conf")
            configuration.write_text(
                "XBPS_ALLOW_CHROOT_BREAKOUT=yes\nXBPS_ALLOW_RESTRICTED=yes\nXBPS_CHROOT_CMD=ethereal\n")
            native("cp", "/output/" + configuration.name, tree + "/etc/conf")

            pending, inspected, runtime_requirements = [package], set(), {}
            while pending:
                template = pending.pop(0)
                if template in inspected:
                    continue
                inspected.add(template)
                description = native("./xbps-src", "-p", "hostmakedepends,makedepends,depends",
                                     "show", template, cwd=tree).stdout
                requirements = [line.split(":", 1)[1].strip() for line in description.splitlines()
                                if line.startswith(("hostmakedepends:", "makedepends:"))]
                if requirements:
                    groups[package + "/" + template] = sorted(set(requirements))
                if recipe.get("build_dependencies"):
                    runtime_requirements[template] = [line.split(":", 1)[1].strip()
                                                      for line in description.splitlines() if line.startswith("depends:")]
                    dependencies = native("./xbps-src", "show-build-deps", template, cwd=tree).stdout.split()
                    for dependency in dependencies:
                        # A subpackage can share its parent source template.
                        # Inspect build dependencies by source identity. XBPS
                        # resolves binary dependencies when installing packages.
                        details = native("./xbps-src", "show", dependency, cwd=tree).stdout
                        fields = dict(line.split(":", 1) for line in details.splitlines() if ":" in line)
                        version = fields["version"].strip() + "_" + fields["revision"].strip()
                        available = native("xbps-query", "--rootdir", "/output/root", "--ignore-conf-repos",
                                           "--repository", toolchain["repository"], "-R", "-p", "pkgver",
                                           fields["pkgname"].strip() + "-" + version, check=False)
                        if available.returncode:
                            pending.append(dependency)
            for template, requirements in runtime_requirements.items():
                binary = []
                for requirement in requirements:
                    dependency = requirement
                    if any(comparator in requirement for comparator in ("<", ">", "=")):
                        dependency = native("xbps-uhelper", "getpkgdepname", requirement).stdout.strip()
                    parent = native("readlink", "-f", tree + "/srcpkgs/" + dependency, check=False).stdout.strip().rsplit("/", 1)[-1]
                    if parent not in inspected:
                        binary.append(requirement)
                if binary:
                    group = package + "/" + template
                    groups[group] = sorted(set(groups.get(group, []) + binary))
            sources[package] = sorted(inspected)

        packages, bootstrap, transactions = {}, {}, {}
        cache = directory / "cache"
        cache.mkdir()
        for number, (group, roots) in enumerate(groups.items()):
            root = "/output/transactions/" + str(number)
            native("mkdir", "-p", root + "/var/db")
            native("cp", "-a", "/output/root/var/db/xbps", root + "/var/db/")
            plan = xbps("xbps-install", "--dry-run", *roots, root=root)
            selected = transaction_packages(plan, metadata)
            transactions[group] = selected
            if group == "bootstrap":
                bootstrap = selected.copy()
            for key, entry in selected.items():
                if key in packages and packages[key] != entry:
                    raise RuntimeError(f"Conflicting archive versions across build transactions: {key}")
                packages[key] = entry
            # Reuse only matching archive contents. XBPS still verifies remote
            # signatures when performing the native download-only transaction.
            for entry in selected.values():
                filename = entry["version"] + "." + entry["architecture"] + ".xbps"
                if (cache / filename).exists():
                    continue
                for path in (project / ".work/package-builds").glob("*/tool-cache/" + filename):
                    if hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]:
                        shutil.copyfile(path, cache / filename)
                        signature = Path(str(path) + ".sig2")
                        if signature.exists():
                            shutil.copyfile(signature, cache / signature.name)
                        break
            xbps("xbps-install", "--download-only", "--yes", "--cachedir", "/output/cache", *roots, root=root)

        manifest = {"architecture": toolchain["architecture"], "packages": packages}
        (directory / "tools.json").write_text(json.dumps(manifest, indent=2) + "\n")
        subprocess.run([sys.executable, str(project / "scripts/prepare-packages.py"),
                        str(directory / "tools.json"), str(directory / "repository"),
                        "--cache=" + str(cache)], check=True, capture_output=True, text=True)
        for number, (group, roots) in enumerate(groups.items()):
            plan = xbps("xbps-install", "--dry-run", *roots,
                        root="/output/transactions/" + str(number), repository="/output/repository")
            verified = transaction_packages(plan, {}, {"/output/repository": packages})
            if verified != transactions[group]:
                raise RuntimeError(f"Locked archives changed the native build transaction: {group}")
        pins = dict(sorted((entry["version"], entry["sha256"]) for entry in packages.values()))
        old = toolchain["packages"]
        toolchain["packages"] = pins
        if "bootstrap_packages" in toolchain:
            toolchain["bootstrap_packages"] = {entry["version"]: entry["sha256"] for entry in bootstrap.values()}
        changes = {"added": sorted(pins.keys() - old.keys()), "removed": sorted(old.keys() - pins.keys()),
                   "checksums": sorted(key for key in pins.keys() & old.keys() if pins[key] != old[key])}
        return {"groups": groups, "sources": sources, "indexes": indexes, "changes": changes,
                "archives": len(pins), "changed": pins != old}
    finally:
        subprocess.run(["docker", "exec", container, "chown", "-R",
                        f"{directory.stat().st_uid}:{directory.stat().st_gid}", "/output"],
                       capture_output=True)
        subprocess.run(["docker", "rm", "--force", container], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--packages", nargs="*")
    args = parser.parse_args()
    original = args.inputs.read_bytes()
    declarations = yaml.safe_load(original)
    selected = set(declarations["packages"] if args.packages is None else args.packages)
    if selected - declarations["packages"].keys():
        raise RuntimeError("Tool refresh requires declared recipes")
    work = args.directory.resolve()
    work.mkdir(parents=True)
    names = {declarations["packages"][package]["toolchain"] for package in selected}
    reports, rebuild = {}, set()
    for name in sorted(names):
        recipes = [package for package, recipe in declarations["packages"].items() if recipe["toolchain"] == name]
        reports[name] = refresh(args.project.resolve(), work, declarations, name, recipes)
        if reports[name]["changed"]:
            rebuild.update(recipes)
    args.inputs.write_text(yaml.safe_dump(declarations, sort_keys=False))
    report = {"inputs_sha256": hashlib.sha256(original).hexdigest(),
              "toolchains": reports, "rebuild": sorted(rebuild)}
    (work.parent / "tool-changes.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
