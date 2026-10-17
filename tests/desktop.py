#!/usr/bin/env python3
"""Check provisioned COSMIC settings through the disposable guest's native readers."""

import argparse
import base64
from collections import Counter
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

from qemu import STATE, ssh_command


GUEST_CHECK = r'''
import json, os, re, shlex, shutil, signal, subprocess, tempfile
from pathlib import Path

inputs = json.loads(input())
pid = subprocess.check_output(["pgrep", "-x", "cosmic-panel"], text=True).strip()
for entry in Path("/proc/" + pid + "/environ").read_bytes().split(b"\0"):
    if b"=" in entry:
        key, value = entry.split(b"=", 1)
        if key in (b"DBUS_SESSION_BUS_ADDRESS", b"XDG_RUNTIME_DIR", b"WAYLAND_DISPLAY"):
            os.environ[key.decode()] = value.decode()
os.environ["RUST_LOG"] = "info"
home = Path.home()
preferences = inputs["preferences"]
directory = Path(tempfile.mkdtemp(prefix="wdc-desktop-"))
logs, reads = {}, []
applications = set(key.split("/")[0] for key in preferences)
# Settings reads the native panel and theme builders as well as its own keys.
applications.add("com.system76.CosmicSettings")
readers = [(application, []) for application in sorted(applications) if application != "com.system76.CosmicSettings"]
readers += [("com.system76.CosmicSettings", [page]) for page in ("appearance", "sound", "power")]
if inputs.get("factory"):
    readers.append(("cosmic-greeter", []))
if inputs.get("override"):
    key = next(iter(inputs["override"]))
    application = key.split("/")[0]
    if not (Path("/usr/share/applications") / (application + ".desktop")).is_file():
        application = "com.system76.CosmicFiles"
    readers = [(application, [])]
for application, arguments in readers:
    desktop = Path("/usr/share/applications") / (application + ".desktop")
    if not desktop.is_file() and application != "cosmic-greeter":
        continue
    command = "cosmic-greeter" if application == "cosmic-greeter" else next(
        line[5:] for line in desktop.read_text().splitlines() if line.startswith("Exec="))
    command = [word for word in shlex.split(command) if not word.startswith("%")]
    if application == "com.system76.CosmicTerm":
        command.insert(1, "--no-daemon")
    command += arguments
    label = application + ("-" + arguments[0] if arguments else "")
    environment = os.environ.copy()
    if inputs.get("override") or inputs.get("factory"):
        root = directory / "config"
        for key, value in ({} if inputs.get("factory") else preferences | inputs["override"]).items():
            path = root / "cosmic" / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inputs["override"].get(key, value))
        environment["XDG_CONFIG_HOME"] = str(root)
    if application == "cosmic-greeter":
        # Its locker reads configuration before creating its event loop. No
        # display connection is supplied, so this control cannot lock the guest.
        for variable in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY"):
            environment.pop(variable, None)
    trace = directory / (label + ".reads")
    with (directory / (label + ".log")).open("w") as output:
        process = subprocess.Popen(["strace", "-f", "-e", "trace=openat", "-o", str(trace), *command],
                                   stdout=output, stderr=subprocess.STDOUT, env=environment, start_new_session=True)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    logs[label] = (directory / (label + ".log")).read_text()
    reads.append(trace.read_text())
if not inputs.get("override") and not inputs.get("factory"):
    logs["session"] = (home / "cosmic-session.log").read_text()
    trace = home / "cosmic-config-reads.log"
    if trace.is_file():
        reads.append(trace.read_text())
trace = "\n".join(reads)
completed, pending = [], {}
for line in trace.splitlines():
    pid = line.split()[0] if line.split() else ""
    if "<unfinished ...>" in line:
        pending[pid] = line.split("<unfinished ...>")[0]
    elif "<... openat resumed>" in line:
        completed.append(pending.pop(pid, "") + line.split("<... openat resumed>")[1])
    else:
        completed.append(line)
opened = sorted(key for key in preferences if any(
    "/cosmic/" + key + '"' in line and re.search(r"= [0-9]+", line)
    for line in completed))
root = Path("/usr/share/cosmic")
defaults = {str(path.relative_to(root)): path.read_text() for path in sorted(root.rglob("*")) if path.is_file()}
configuration_reads = [line for line in completed if any('/cosmic/' + key + '"' in line for key in preferences)]
shutil.rmtree(directory)
print(json.dumps({"logs": logs, "read_preferences": opened, "defaults": defaults,
                  "configuration_reads": configuration_reads,
                  "packages": subprocess.check_output(["xbps-query", "-l"], text=True)}))
'''


def check(preferences, override=None, factory=False):
    # Send inputs on stdin. Shell command text contains neither preference values
    # nor private host configuration, and the guest supplies its own session bus.
    code = "import base64; exec(base64.b64decode(" + repr(base64.b64encode(GUEST_CHECK.encode()).decode()) + "))"
    result = subprocess.run(ssh_command(["python3 -c " + shlex.quote(code)]),
                            input=json.dumps({"preferences": preferences, "override": override, "factory": factory}) + "\n",
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr)
    captured = json.loads(result.stdout)
    errors, gaps = [], []
    for reader, log in captured["logs"].items():
        for line in log.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            if re.search(r"Ron(?:Spanned)?\(|SpannedError|missing struct field|Expected[A-Z]", line):
                errors.append({"reader": reader, "message": line})
            elif "GetKey(" in line and ("NotFound" in line or "No such file" in line):
                gaps.append({"reader": reader, "message": line})
    captured["parse_errors"] = errors
    captured["package_default_gaps"] = gaps
    captured["unread_preferences"] = sorted(preferences.keys() - set(captured["read_preferences"]))
    return captured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, help="Update directory containing configuration-before/after")
    parser.add_argument("--prove-rejection", action="store_true", help="Exercise malformed values in isolated user-config directories")
    args = parser.parse_args()
    preferences = json.loads((STATE / "preferences.json").read_text())
    result = check(preferences)
    factory = check(preferences, factory=True)
    # Run the same readers with no user preferences to distinguish errors in
    # package defaults from errors in the configured preferences.
    def error_signature(error):
        message = error["message"]
        start = re.search(r"Ron(?:Spanned)?\(|SpannedError|missing struct field|Expected[A-Z]", message)
        reader = error["reader"]
        if reader == "session" and "cosmic-greeter" in message:
            reader = "cosmic-greeter"
        return reader, message[start.start():] if start else message
    factory_errors = Counter(error_signature(error) for error in factory["parse_errors"])
    result["package_default_parse_errors"], authored_errors = [], []
    for error in result["parse_errors"]:
        signature = error_signature(error)
        if factory_errors[signature]:
            factory_errors[signature] -= 1
            result["package_default_parse_errors"].append(error)
        else:
            authored_errors.append(error)
    result["parse_errors"] = authored_errors
    result["factory_logs"] = factory["logs"]
    if args.candidate:
        for key in preferences:
            before = args.candidate / "configuration-before/usr/share/cosmic" / key
            after = args.candidate / "configuration-after/usr/share/cosmic" / key
            if before.is_file() and not after.is_file():
                result["parse_errors"].append({"reader": "package defaults", "message": "Removed native key or schema version: " + key})
    if args.prove_rejection:
        result["rejection_checks"] = {}
        cases = {
            "nested_struct": {"com.system76.CosmicFiles/v1/tab": '(icon_sizes: (list: "invalid-integer"))'},
            "enum": {"com.system76.CosmicFiles/v1/tab": '(view: UnknownView)'},
            "required_field": {"com.system76.CosmicTheme.Dark/v2/spacing": '(space_none: 0)'},
        }
        for name, override in cases.items():
            rejected = check(preferences, override)
            result["rejection_checks"][name] = rejected["parse_errors"]
            if not rejected["parse_errors"] or not set(override).issubset(rejected["read_preferences"]):
                result["parse_errors"].append({"reader": "rejection check", "message": "Native read and rejection were not observed for " + name})
    destination = STATE / "desktop-validation.json"
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Native readers opened {len(result['read_preferences'])}/{len(preferences)} provisioned preferences")
    print(f"Parse errors: {len(result['parse_errors'])}; package-default gaps: {len(result['package_default_gaps'])}")
    print(f"Parse errors reproduced with factory configuration: {len(result['package_default_parse_errors'])}")
    print(f"Evidence: {destination}")
    if result["unread_preferences"]:
        print("Unread preferences: " + ", ".join(result["unread_preferences"]))
    if result["parse_errors"] or result["unread_preferences"]:
        raise RuntimeError("Configuration was rejected or reader coverage is incomplete; inspect desktop-validation.json")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error))
