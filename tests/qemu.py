#!/usr/bin/env python3
"""Launch and inspect the disposable guest assembled by tests/vm.yml."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys


PROJECT = Path(__file__).resolve().parent.parent
STATE = PROJECT / ".work/qemu"


def monitor(command, arguments=None):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(str(STATE / "monitor.sock"))
        stream = connection.makefile("rwb")
        json.loads(stream.readline())
        for name, values in [("qmp_capabilities", {}), (command, arguments or {})]:
            stream.write(json.dumps({"execute": name, "arguments": values}).encode() + b"\n")
            stream.flush()
            while True:
                reply = json.loads(stream.readline())
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                if "return" in reply:
                    break
        return reply["return"]


def start(args):
    if (STATE / "qemu.pid").exists():
        raise RuntimeError("A VM PID file exists; stop that guest before starting another")
    if args.fresh:
        for name in ('root.qcow2', 'efi.qcow2', 'OVMF_VARS.fd', 'known_hosts'):
            (STATE / name).unlink(missing_ok=True)
    image = (args.image or STATE / "image").resolve()
    verified = subprocess.run([sys.executable, str(PROJECT / "scripts/image.py"), 'verify', str(image)],
                              capture_output=True, text=True)
    if verified.returncode:
        raise RuntimeError(verified.stderr.strip())
    build = json.loads((image / "manifest.json").read_text())
    if build['kind'] == 'test' and not (STATE / 'id_ed25519').is_file():
        raise RuntimeError('The test image requires its VM-only SSH identity')
    STATE.mkdir(parents=True, exist_ok=True)
    # Run the guest from overlays so its verified base disks remain unchanged.
    for name in ('root', 'efi'):
        overlay = STATE / (name + '.qcow2')
        if overlay.exists():
            raise RuntimeError('Runtime overlays exist; use --fresh to discard them')
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'raw', '-b',
                        str(image / (name + '.img')), str(overlay)], check=True, capture_output=True)
    (STATE / 'build.json').write_text(json.dumps(build) + '\n')

    packages = json.loads((PROJECT / "locks/packages.json").read_text())["packages"]
    for name in ["qemu", "edk2-ovmf"]:
        installed = subprocess.run(
            ["xbps-query", "-p", "pkgver", name], capture_output=True, text=True,
        )
        if installed.returncode or installed.stdout.strip() != packages[name]["version"]:
            raise RuntimeError(f"The VM controller requires locked {packages[name]['version']}")
    qemu_version = packages["qemu"]["version"].removeprefix("qemu-").split("_")[0]
    machine = "pc-q35-" + ".".join(qemu_version.split(".")[:2])

    firmware = Path("/usr/share/edk2/x64")
    if not (STATE / "OVMF_VARS.fd").exists():
        shutil.copyfile(firmware / "OVMF_VARS.fd", STATE / "OVMF_VARS.fd")

    display = ["-device", "virtio-vga-gl", "-display", "gtk,gl=on"]
    if args.headless:
        display = ["-device", "virtio-vga-gl", "-display", f"egl-headless,rendernode={args.render_node}"]

    audio = "pipewire,id=audio"
    if args.record_audio:
        audio = f"wav,id=audio,path={STATE / 'audio.wav'}"

    if args.console:
        display = ['-device', 'virtio-vga', '-display', 'none']
        audio = 'none,id=audio'

    command = [
        "qemu-system-x86_64", "-name", "wdc-server", "-machine", f"{machine},accel=kvm",
        "-cpu", "host", "-smp", "4", "-m", "8192", "-nodefaults",
        "-drive", f"if=pflash,format=raw,readonly=on,file={firmware / 'OVMF_CODE.fd'}",
        "-drive", f"if=pflash,format=raw,file={STATE / 'OVMF_VARS.fd'}",
        "-drive", f"if=virtio,format=qcow2,file={STATE / 'efi.qcow2'}",
        "-drive", f"if=virtio,format=qcow2,file={STATE / 'root.qcow2'}",
        "-netdev", f"user,id=network,hostfwd=tcp:127.0.0.1:{args.ssh_port}-:22",
        "-device", "virtio-net-pci,netdev=network",
        "-device", "qemu-xhci", "-device", "usb-tablet", "-device", "usb-kbd",
        "-audiodev", audio, "-device", "intel-hda", "-device", "hda-duplex,audiodev=audio",
        "-serial", f"file:{STATE / 'serial.log'}",
        "-qmp", f"unix:{STATE / 'monitor.sock'},server=on,wait=off",
        "-pidfile", str(STATE / "qemu.pid"), "-daemonize", *display,
    ]
    with (STATE / "qemu.log").open("w") as output:
        (STATE / "serial.log").write_text("")
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError((STATE / "qemu.log").read_text())
    (STATE / "connection.json").write_text(json.dumps({"ssh_port": args.ssh_port}) + "\n")
    print(f"Guest started. Serial log: {STATE / 'serial.log'}")


def ssh_command(command):
    port = json.loads((STATE / "connection.json").read_text())["ssh_port"]
    build = json.loads((STATE / "build.json").read_text())
    if build['kind'] != 'test':
        raise RuntimeError('Release images have no VM test authorization; use the serial console')
    user = build['user']
    # Host keys are retained per disposable image, without changing the host's SSH files.
    return [
        "ssh", "-i", str(STATE / "id_ed25519"), "-p", str(port),
        "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={STATE / 'known_hosts'}", "-o", "ConnectTimeout=10",
        f"{user}@127.0.0.1", *command,
    ]


def screenshot():
    destination = STATE / "desktop.png"
    try:
        monitor("screendump", {"filename": str(destination), "format": "png"})
    except RuntimeError as error:
        if "no surface" not in str(error):
            raise
        # VirGL scanout may lack a CPU surface for QMP. Use the guest screenshot
        # portal in that case. It may ask for permission.
        capture = '''
import base64, os, subprocess, tempfile
from pathlib import Path
pid = subprocess.check_output(["pgrep", "-x", "cosmic-comp"], text=True).strip()
for entry in Path("/proc/" + pid + "/environ").read_bytes().split(b"\\0"):
    if b"=" in entry:
        key, value = entry.split(b"=", 1)
        if key in [b"DBUS_SESSION_BUS_ADDRESS", b"XDG_RUNTIME_DIR"]:
            os.environ[key.decode()] = value.decode()
with tempfile.TemporaryDirectory() as directory:
    subprocess.run(["cosmic-screenshot", "--interactive=false", "--notify=false",
                    "--save-dir=" + directory], check=True, stdout=subprocess.DEVNULL,
                   timeout=45)
    images = list(Path(directory).glob("*.png"))
    if len(images) != 1:
        raise RuntimeError("The guest screenshot did not produce one PNG")
    print(base64.b64encode(images[0].read_bytes()).decode())
'''
        result = subprocess.run(
            ssh_command(["python3 -"]), input=capture, text=True,
            capture_output=True, timeout=55,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        destination.write_bytes(base64.b64decode(result.stdout.strip(), validate=True))
    print(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    launch = commands.add_parser("start")
    launch.add_argument('--image', type=Path, help='Boot a verified release directory; defaults to the test image')
    launch.add_argument('--fresh', action='store_true', help='Discard previous disposable disks before booting')
    launch.add_argument('--console', action='store_true', help='Boot without graphical or audio host access')
    launch.add_argument("--headless", action="store_true", help="Render through EGL without opening a window")
    launch.add_argument("--render-node", default="/dev/dri/renderD128", help="Controller render node for headless EGL")
    launch.add_argument("--record-audio", action="store_true", help="Write HDA output to .work/qemu/audio.wav")
    launch.add_argument("--ssh-port", type=int, default=2222)
    commands.add_parser("status")
    commands.add_parser("stop", help="Request orderly ACPI shutdown")
    commands.add_parser("quit", help="Stop QEMU immediately if guest shutdown failed")
    commands.add_parser("screenshot")
    remote = commands.add_parser("ssh")
    remote.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.operation == "start":
        start(args)
    elif args.operation == "ssh":
        command = ssh_command(args.command)
        os.execvp(command[0], command)
    elif args.operation == "status":
        print(json.dumps(monitor("query-status"), indent=2))
    elif args.operation == "stop":
        monitor("system_powerdown")
        print("Requested guest shutdown. Wait for .work/qemu/qemu.pid to disappear before rebuilding.")
    elif args.operation == "quit":
        monitor("quit")
    elif args.operation == "screenshot":
        screenshot()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
