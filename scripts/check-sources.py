#!/usr/bin/env python3
"""Compare selected package templates with their repositories' current revisions."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import posixpath
import subprocess
import sys
import urllib.request

import yaml


def git(repository, *arguments):
    return subprocess.check_output(
        ["git", "--git-dir", str(repository), *arguments], stderr=subprocess.PIPE,
    ).decode()


def check_repository(directory, source):
    subprocess.run(["git", "init", "--bare", "--quiet", str(directory)], check=True)
    git(directory, "remote", "add", "origin", source["repository"])
    # Comparing two complete trees needs no intervening commit history. Blob
    # contents are fetched only when Git needs them for a selected template.
    git(directory, "fetch", "--quiet", "--depth=1", "--filter=blob:none", "--no-tags",
        "origin", source["commit"] + ":refs/check/previous",
        source.get("ref", "HEAD") + ":refs/check/current")
    current = git(directory, "rev-parse", "refs/check/current").strip()
    files = git(directory, "diff", "--no-renames", "--name-only", "-z",
                "refs/check/previous", "refs/check/current").rstrip("\0").split("\0")
    return {"from": source["commit"], "to": current, "files": sorted(filter(None, files))}


def template_paths(repository, revisions, names):
    paths = {"srcpkgs/" + name for name in names}
    # Void subpackages can be symlinks to a parent template. Include that parent
    # in both revisions so a changed target or parent recipe is also detected.
    for revision in revisions:
        if not paths:
            continue
        for entry in git(repository, "ls-tree", revision, "--", *sorted(paths)).splitlines():
            if entry.startswith("120000 "):
                path = entry.split("\t", 1)[1]
                target = git(repository, "show", revision + ":" + path).strip()
                paths.add(posixpath.normpath(str(Path(path).parent / target)))
    return paths


def download(url, destination):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "wdc-server-update"},
    )
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while data := response.read(1024 * 1024):
            output.write(data)
            digest.update(data)
    return digest.hexdigest()


def patch_templates(project, work, name, patch):
    cached = project / ".work/package-builds" / name / "pull-request.diff"
    if not cached.exists() or hashlib.sha256(cached.read_bytes()).hexdigest() != patch["sha256"]:
        cached = work / (name + "-pull-request.diff")
        if download(patch["url"], cached) != patch["sha256"]:
            raise RuntimeError(f"Published patch differs from its pin: {name}")
    return {line.split("/")[2] for line in cached.read_text().splitlines()
            if line.startswith(("+++ b/srcpkgs/", "--- a/srcpkgs/"))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--packages", nargs="*")
    parser.add_argument("--candidates", nargs="*", default=[])
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    inputs = yaml.safe_load(args.inputs.read_text())
    selected = set(inputs["packages"] if args.packages is None else args.packages) | set(args.candidates)
    unknown = selected - inputs["packages"].keys()
    if unknown:
        raise RuntimeError(f"Undeclared build recipes: {sorted(unknown)}")
    work = args.directory.resolve()
    repositories = work / "repositories"
    archives = work / "source-archives"
    repositories.mkdir(parents=True)
    archives.mkdir()
    source_names = {inputs["packages"][name][key] for name in selected
                    for key in ("source", "overlay") if key in inputs["packages"][name]}
    checks = {name: check_repository(repositories / (name + ".git"), inputs["sources"][name])
              for name in sorted(source_names)}
    reasons = {}
    advanced = set()
    project = args.project.resolve()
    for name in sorted(selected):
        recipe = inputs["packages"][name]
        templates = {name}
        if "pull_request" in recipe:
            templates.update(patch_templates(project, work, name, recipe["pull_request"]))
        for kind in ("source", "overlay"):
            if kind not in recipe:
                continue
            source_name = recipe[kind]
            check = checks[source_name]
            if not check["files"]:
                continue
            repository = repositories / (source_name + ".git")
            if kind == "overlay":
                watched = {name}
            elif "overlay" in recipe:
                watched = set()
            else:
                watched = templates
            paths = template_paths(repository, [check["from"], check["to"]], watched)
            changed = [path for path in check["files"]
                       if any(path == prefix or path.startswith(prefix + "/") for prefix in paths)
                       or (kind == "source" and
                           (path == "xbps-src" or path.startswith(("common/", "etc/"))))]
            if changed:
                reasons.setdefault(name, []).append({"source": source_name, "files": changed})
                advanced.add(source_name)

    # Explicitly selected cached builds must agree with the proposed shared
    # source pins, even when another recipe caused those pins to advance.
    for name in args.candidates:
        recipe = inputs["packages"][name]
        if any(recipe.get(key) in advanced for key in ("source", "overlay")):
            reasons.setdefault(name, []).append({"reason": "Selected artifact needs the proposed source revisions"})

    proposed = copy.deepcopy(inputs)
    for name in sorted(advanced):
        source = proposed["sources"][name]
        source["commit"] = checks[name]["to"]
        source["sha256"] = download(source["archive_url"] + "/" + source["commit"],
                                    archives / (name + "-" + source["commit"] + ".tar.gz"))
    report = {"sources": checks, "advanced": sorted(advanced),
              "rebuild": sorted(reasons), "reasons": reasons,
              "inputs_sha256": hashlib.sha256(args.inputs.read_bytes()).hexdigest()}
    (work / "source-changes.json").write_text(json.dumps(report, indent=2) + "\n")
    (work / "builds.yml").write_text(yaml.safe_dump(proposed, sort_keys=False))
    print(json.dumps({"rebuild": report["rebuild"], "advanced": report["advanced"]}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        detail = error.stderr.decode() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        sys.exit(detail)
