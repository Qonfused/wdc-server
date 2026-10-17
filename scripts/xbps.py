"""Read native XBPS repository indexes and transactions without resolving dependencies."""

import plistlib
import subprocess


def repository_index(container, path):
    data = subprocess.check_output(["docker", "exec", container, "bsdtar",
                                    "-xOf", path, "index.plist"])
    return plistlib.loads(data)


def transaction_packages(plan, metadata, local_repositories=None):
    packages = {}
    local_repositories = local_repositories or {}
    for line in plan.splitlines():
        fields = line.split()
        if len(fields) != 6 or fields[1] not in ("install", "remove"):
            raise RuntimeError(f"Unexpected XBPS transaction row: {line}")
        version, action, architecture, repository, _, _ = fields
        name = version.rsplit("-", 1)[0]
        if action == "remove":
            packages.pop(name, None)
            continue
        if repository in local_repositories:
            entry = local_repositories[repository][name].copy()
            if entry["version"] != version or entry["architecture"] != architecture:
                raise RuntimeError(f"Selected local artifact differs from its pin: {version}")
        else:
            matches = [entry for entry in metadata.get(version, [])
                       if entry["architecture"] == architecture]
            digests = {entry["filename-sha256"] for entry in matches}
            if len(digests) != 1:
                raise RuntimeError(f"Ambiguous or absent repository checksum: {version}")
            entry = {"version": version, "architecture": architecture,
                     "sha256": digests.pop(),
                     "url": f"{repository}/{version}.{architecture}.xbps"}
        packages[name] = entry
    return packages


def cosmic_defaults(container, repository, packages, destination):
    """Extract native defaults from already checksum-verified package archives."""
    native_destination = "/output/" + destination.name
    subprocess.run(["docker", "exec", container, "mkdir", "-p", native_destination], check=True)
    for entry in packages.values():
        archive = entry["version"] + "." + entry["architecture"] + ".xbps"
        path = repository + "/" + archive
        members = subprocess.check_output(["docker", "exec", "--env", "LC_ALL=C.UTF-8", container,
                                           "bsdtar", "-tf", path], text=True)
        selected = [member for member in members.splitlines()
                    if member.removeprefix("./").startswith("usr/share/cosmic/")]
        if selected:
            subprocess.run(["docker", "exec", "--env", "LC_ALL=C.UTF-8", container, "bsdtar", "-xf", path,
                            "-C", native_destination, "--no-same-owner", *selected],
                           check=True, capture_output=True)
    root = destination / "usr/share/cosmic"
    return {str(path.relative_to(root)): path.read_text()
            for path in sorted(root.rglob("*")) if path.is_file()}
