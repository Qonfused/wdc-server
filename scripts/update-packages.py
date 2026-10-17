#!/usr/bin/env python3
"""Use native XBPS transactions to produce a verified candidate package lock."""

import argparse
import difflib
import fnmatch
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from xbps import cosmic_defaults, repository_index, transaction_packages


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def verify_build(candidate, declarations):
    build = candidate["build"]
    recipe = declarations["packages"][build["package"]]
    if build["recipe"] != recipe:
        raise RuntimeError(f"Rebuild {build['package']}: recipe pins have changed")
    sources = [(build["source"], declarations["sources"][recipe["source"]])]
    if "overlay" in recipe:
        sources.append((build["overlay"], declarations["sources"][recipe["overlay"]]))
    for actual, expected in sources:
        if any(actual[key] != expected[key] for key in ("commit", "sha256")):
            raise RuntimeError(f"Rebuild {build['package']}: source pins have changed")
    toolchain = declarations["toolchains"][recipe["toolchain"]]
    if any(build["toolchain"][key] != toolchain[key]
           for key in ("image", "packages", "architecture", "platform", "repository")):
        raise RuntimeError(f"Rebuild {build['package']}: build environment pins have changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("container")
    args = parser.parse_args()
    work = args.directory.resolve()
    inputs = json.loads((work / "input.json").read_text())
    baseline = json.loads(Path(inputs["lock"]).read_text())
    selection = json.loads((work / "selection.json").read_text())
    roots = set(selection["packages"] + inputs["extra_packages"])
    keep_patterns = selection["keep"] if inputs["keep"] is None else inputs["keep"]
    logs = work / "native.log"

    def native(program, *arguments, repositories=None):
        command = ["docker", "exec", args.container, program]
        if program in ("xbps-install", "xbps-query"):
            command += ["--rootdir", "/output/root", "--ignore-conf-repos"]
            for repository in repositories or inputs["repositories"]:
                command += ["--repository", repository]
        result = subprocess.run(command + list(arguments), capture_output=True, text=True)
        with logs.open("a") as stream:
            stream.write("$ " + " ".join(command + list(arguments)) + "\n")
            stream.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"{program} failed ({result.returncode}):\n{result.stdout}{result.stderr}")
        return result.stdout

    root_database = work / "root/var/db/xbps"
    root_database.mkdir(parents=True, exist_ok=True)
    native("cp", "-a", "/var/db/xbps/keys", "/output/root/var/db/xbps/")
    native("xbps-install", "--sync")

    # Cached indexes retain the metadata selected for this candidate. Subsequent
    # commands use those indexes without synchronizing against a moving mirror.
    indexes = []
    metadata = {}
    for path in sorted(root_database.rglob("*-repodata")):
        index = repository_index(args.container, "/output/" + str(path.relative_to(work)))
        for entry in index.values():
            metadata.setdefault(entry["pkgver"], []).append(entry)
        indexes.append({"file": str(path.relative_to(work)),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not indexes:
        raise RuntimeError("XBPS did not retain any repository indexes")

    kept = {name for name in baseline["packages"]
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in keep_patterns)}
    unmatched = [pattern for pattern in keep_patterns
                 if not any(fnmatch.fnmatchcase(name, pattern) for name in baseline["packages"])]
    if unmatched:
        raise RuntimeError(f"Retention patterns match no baseline packages: {unmatched}")
    local = {name: baseline["packages"][name] for name in kept}
    builds = []
    for filename in inputs["candidates"]:
        candidate = json.loads(Path(filename).read_text())
        verify_build(candidate, inputs["build_inputs"])
        if candidate["architecture"] != baseline["architecture"]:
            raise RuntimeError(f"Candidate architecture differs: {filename}")
        conflicts = kept.intersection(candidate["packages"])
        if conflicts:
            raise RuntimeError(f"Built candidate overrides retained packages: {sorted(conflicts)}")
        local.update(candidate["packages"])
        roots.add(candidate["build"]["package"])
        builds.append(candidate["build"])

    source_lock = {"architecture": baseline["architecture"], "packages": local}
    write_json(work / "sources.json", source_lock)
    prepare = Path(inputs["project"]) / "scripts/prepare-packages.py"
    caches = ["--cache=" + str(path) for path in inputs["caches"]]
    if local:
        subprocess.run([sys.executable, str(prepare), str(work / "sources.json"),
                        str(work / "sources"), *caches], check=True, capture_output=True, text=True)
    subprocess.run([sys.executable, str(prepare), inputs["lock"],
                    str(work / "baseline"), *caches], check=True, capture_output=True, text=True)
    repositories = ((["/output/sources"] if local else []) + inputs["repositories"]
                    + ["/output/baseline"])
    local_repositories = {"/output/sources": local, "/output/baseline": baseline["packages"]}

    # The transaction includes dependencies selected by XBPS, including shared
    # libraries recorded in binary package metadata rather than template lists.
    plan = native("xbps-install", "--dry-run", *sorted(roots), repositories=repositories)
    (work / "transaction.txt").write_text(plan)
    packages = transaction_packages(plan, metadata, local_repositories)

    if not roots.issubset(packages):
        raise RuntimeError(f"Unresolved package roots: {sorted(roots - packages.keys())}")
    if not packages:
        raise RuntimeError("The proposed transaction is empty")
    for name in kept.intersection(packages):
        if packages[name] != baseline["packages"][name]:
            raise RuntimeError(f"XBPS did not retain the requested pin: {name}")

    cache = work / "cache"
    cache.mkdir(exist_ok=True)
    # Seed exact matches from existing snapshots, then let XBPS verify downloaded
    # remote archives and their signatures against the pinned environment's keys.
    filenames = {f"{entry['version']}.{entry['architecture']}.xbps": entry
                 for entry in packages.values()}
    for directory in inputs["caches"]:
        for path in Path(directory).rglob("*.xbps"):
            if path.name not in filenames or (cache / path.name).exists():
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest == filenames[path.name]["sha256"]:
                shutil.copyfile(path, cache / path.name)
                signature = Path(str(path) + ".sig2")
                if signature.exists():
                    shutil.copyfile(signature, cache / signature.name)
    native("xbps-install", "--download-only", "--yes", "--cachedir", "/output/cache",
           *sorted(roots), repositories=repositories)

    lock = {"baseline": work.name, "architecture": baseline["architecture"],
            "kernel_package": baseline["kernel_package"], "packages": dict(sorted(packages.items()))}
    write_json(work / "verified-inputs.json", lock)
    subprocess.run([sys.executable, str(prepare), str(work / "verified-inputs.json"),
                    str(work / "packages"), "--cache=" + str(cache),
                    "--cache=" + str(work / "sources"),
                    "--cache=" + str(work / "baseline")], check=True, capture_output=True, text=True)
    # Verify that the retained archives alone satisfy the same transaction.
    native("xbps-install", "--dry-run", *sorted(roots), repositories=["/output/packages"])

    changes = {"added": {}, "changed": {}, "removed": {}}
    for name in sorted(packages.keys() | baseline["packages"].keys()):
        old, new = baseline["packages"].get(name), packages.get(name)
        if old is None:
            changes["added"][name] = new["version"]
        elif new is None:
            changes["removed"][name] = old["version"]
        elif any(old[key] != new[key] for key in ("version", "architecture", "sha256")):
            changes["changed"][name] = {"from": old["version"], "to": new["version"],
                                        "sha256_changed": old["sha256"] != new["sha256"]}
    write_json(work / "changes.json", changes)
    # Compare package-owned defaults, including their native version directories.
    # The VM separately exercises consumers because these files are not a full
    # schema: applications also supply defaults compiled into their binaries.
    old_defaults = cosmic_defaults(args.container, "/output/baseline", baseline["packages"], work / "configuration-before")
    new_defaults = cosmic_defaults(args.container, "/output/packages", packages, work / "configuration-after")
    configuration = {"added": sorted(new_defaults.keys() - old_defaults.keys()),
                     "removed": sorted(old_defaults.keys() - new_defaults.keys()),
                     "changed": sorted(key for key in old_defaults.keys() & new_defaults.keys()
                                       if old_defaults[key] != new_defaults[key])}
    write_json(work / "configuration-changes.json", configuration)
    with (work / "configuration.diff").open("w") as output:
        for key in sorted(set(configuration["added"] + configuration["removed"] + configuration["changed"])):
            output.writelines(difflib.unified_diff(old_defaults.get(key, "").splitlines(True),
                                                 new_defaults.get(key, "").splitlines(True),
                                                 fromfile="before/" + key, tofile="after/" + key))
    write_json(work / "resolution.json", {"inputs": inputs, "selection": selection,
               "roots": sorted(roots), "kept": sorted(kept.intersection(packages)),
               "indexes": indexes, "builds": builds,
               "xbps_version": native("xbps-install", "--version").strip(),
               "baseline_sha256": hashlib.sha256(Path(inputs["lock"]).read_bytes()).hexdigest()})
    # Write the candidate lock after XBPS verifies the downloaded archives
    # and resolves the same transaction from the local repository.
    shutil.copyfile(work / "verified-inputs.json", work / "packages.json")
    print(f"Candidate: {work / 'packages.json'}")
    print(f"Archives: {work / 'packages'}")
    print(f"Packages: {len(packages)}; added {len(changes['added'])}, "
          f"changed {len(changes['changed'])}, removed {len(changes['removed'])}")
    print(f"Changes: {work / 'changes.json'}")
    print(f"Native configuration changes: {work / 'configuration-changes.json'}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        detail = (error.stderr or error.stdout) if isinstance(error, subprocess.CalledProcessError) else str(error)
        sys.exit(detail)
