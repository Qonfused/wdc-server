# Testing

Use the syntax and archive tests when changing playbooks or build tooling. The Docker scenario checks installation and repeat application of the configuration. For package or desktop upgrades, boot the test image in QEMU and exercise the affected applications before accepting their new locks.

Run commands from the repository root after [setting up the project](../README.md#getting-started).

## Playbooks and Archive Handling

Playbook syntax and the Python tests can be checked without building an image:

```sh
uv sync --locked
uv run --locked python scripts/inputs.py check locks/input-release.json --project .
uv run --locked ansible-playbook prepare.yml --tags collections
uv run --locked ansible-playbook bootstrap.yml --syntax-check
uv run --locked ansible-playbook build/image.yml --syntax-check
uv run --locked ansible-playbook tests/vm.yml --syntax-check
uv run --locked python -m unittest discover -s tests -p 'test_*.py' -v
```

The Python tests supply missing or damaged archives, change checksums, and serve downloads through a local HTTP server to exercise recovery and rejection. They also check bundle contents, lock mismatches, and image manifests. These tests use fixtures. The Docker and QEMU tests below exercise the installed system.

The [validation workflow](../.github/workflows/validate.yml) runs these commands on GitHub Actions and saves the test log. Container installation and VM checks are run locally.

## Installation in Docker

Molecule applies the configuration to a disposable Void container, checks that a second application makes no changes, then modifies the installation and checks that applying it again repairs the drift:

```sh
uv run --locked ansible-playbook prepare.yml
MOLECULE_GLOB='tests/molecule/*/molecule.yml' \
  uv run --locked --group test molecule test
```

The scenario also checks package version enforcement and invalid inputs. Its version-change cases require the earlier archives listed in [previous-package.json](../tests/molecule/default/previous-package.json) to be available in `/var/cache/xbps`. The container has no host devices or network access.

For NVIDIA, the checks examine the compiled DKMS modules, kernel symbols, and initramfs contents. `tests/test_nvidia_power.py` executes the startup policy against simulated GPU identities. Confirming the applied power limit requires a physical GPU.

### Chrome

After [building the Chrome package](maintenance.md#building-an-individual-package), test it with:

```sh
uv run --locked ansible-playbook tests/chrome.yml
```

This installs the candidate in a fresh container with the locked installation dependencies, checks the browser version, and renders a page in headless mode. The result is written to `.work/tests/chrome.json`.

## Desktop in QEMU

The VM launcher runs on a Void host with KVM access and the `qemu` and `edk2-ovmf` versions specified in [locks/packages.json](../locks/packages.json). It checks those installed versions through XBPS before launching. The guest uses four virtual CPUs and 8 GiB of memory. Graphical mode requires a desktop session with OpenGL and PipeWire available on the host.

Build the test image and start it:

```sh
uv run --locked ansible-playbook tests/vm.yml
uv run --locked python tests/qemu.py start --fresh
```

The test image uses the ordinary system configuration with console autologin, an SSH key, and diagnostic tools supplied by [tests/guest.yml](../tests/guest.yml). It does not restore the host's RustDesk identity. QEMU writes guest changes to separate disk overlays, leaving the built image intact. `--fresh` discards previous overlays and starts from the built image again.

After the COSMIC desktop has loaded, validate the preferences:

```sh
uv run --locked python tests/desktop.py
```

The check starts COSMIC's own configuration readers in the guest and traces which preference files they open. It reports rejected values and unread preferences, with errors also present under factory settings recorded separately. Inspect the desktop as well to verify its appearance and behavior.

For an upgrade, pass `--candidate /path/to/candidate` to also check for removed package-default keys and schema directories. See [Testing and Accepting an Update](maintenance.md#testing-and-accepting-an-update) for building that candidate. `--prove-rejection` checks that malformed values are rejected using temporary configuration directories in the guest.

### Interacting with the Guest

```sh
uv run --locked python tests/qemu.py ssh 'uname -r'
uv run --locked python tests/qemu.py screenshot
uv run --locked python tests/qemu.py stop
```

The screenshot command may request screen-sharing permission in the guest. `stop` requests an orderly shutdown. Wait for `.work/qemu/qemu.pid` to disappear before rebuilding or starting another VM. If the guest cannot shut down, `quit` terminates QEMU immediately.

These options change how the VM connects to the host:

| Option to `start` | Behavior |
| --- | --- |
| `--headless` | Renders through an accessible EGL render device without opening a window |
| `--render-node PATH` | Selects the render device for headless mode |
| `--record-audio` | Saves virtual audio output to `.work/qemu/audio.wav` |
| `--console` | Runs without graphical or audio output to the host, recording boot output in the serial log |

VM files are kept in `.work/qemu/`:

| File | Contents |
| --- | --- |
| `desktop-validation.json` | Preference read results, parsing errors, and comparison with factory settings |
| `serial.log` | Guest boot and console output |
| `qemu.log` | QEMU startup output and errors |
| `desktop.png` | Screenshot captured from the guest |
| `id_ed25519` | Private SSH key created for the test VM |

## Release Images

After [building a release image](../README.md#building-an-image), verify its contents and boot it:

```sh
uv run --locked python tests/release.py artifacts/base
uv run --locked python tests/qemu.py start \
  --image "$PWD/artifacts/base" --fresh --console
```

The release check verifies output hashes and checks that passwords are locked, machine identities are absent, and test access settings were excluded. Release images have no VM SSH key or automatic login. Inspect `.work/qemu/serial.log` to confirm that boot reaches the login prompt, then shut down the guest:

```sh
uv run --locked python tests/qemu.py stop
```

QEMU exercises boot, virtual graphics, and virtual audio. Physical GPU initialization, device recovery, and audio hardware must be tested on the intended machine. RustDesk and Tailscale also need service tests to establish that restored identities are accepted by their servers.
