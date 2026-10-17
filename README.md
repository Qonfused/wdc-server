# wdc-server

**wdc-server** is an Ansible configuration for rebuilding and maintaining a Void Linux desktop and Docker host. It includes a COSMIC desktop with PipeWire audio, Intel and NVIDIA graphics, Tailscale, and RustDesk.

The project starts with a Void root filesystem, installs a fixed set of package versions, and applies the settings in [config/](config/). Changes to the configuration and package versions can be tested in QEMU before deployment. The configuration is organized by component, while XBPS and upstream package templates supply package dependencies and build recipes.

## Documentation

- [Configuration](#configuration): select components and change system settings.
- [Maintenance](docs/maintenance.md): update packages, preserve build dependencies, and apply the configuration to a machine.
- [Testing](docs/testing.md): validate installation, boot an image, and check desktop preferences.

## Getting Started

### Prerequisites

Build images on an x86_64 Linux machine with the following tools installed:

| Tool | Purpose |
| --- | --- |
| uv | Installs the project's Python environment and runs Ansible. The required uv version is specified in [pyproject.toml](pyproject.toml). |
| Docker | Runs the Void installation in a build container. Your account must be able to connect to the Docker daemon. |
| XBPS | Provides `xbps-rindex`, which creates the package repository used during installation. |
| mtools | Provides `mformat`, `mmd`, and `mcopy` to assemble the UEFI boot disk. |

See the [QEMU requirements](docs/testing.md#desktop-in-qemu) for running the desktop tests.

Clone the repository and install the Python dependencies:

```sh
git clone https://github.com/Qonfused/wdc-server.git
cd wdc-server
uv sync --locked
```

Run the following commands from the repository root.

### Preparing Dependencies

Image builds use the package archives listed in [locks/packages.json](locks/packages.json). The release assets are pinned by URL, size, and checksum in [locks/input-release.json](locks/input-release.json). Prepare them with:

```sh
uv run --locked ansible-playbook prepare.yml
```

Preparation downloads the release assets, checks their hashes, and verifies every archive against the package, rootfs, and collection locks. It then builds the local XBPS repository under `.work/packages`.

A saved **input bundle** can also be used directly. To use one, replace the path below with its directory:

```sh
inputs=/absolute/path/to/inputs
uv run --locked python scripts/inputs.py verify "$inputs" --project .
uv run --locked ansible-playbook prepare.yml \
  -e artifact_source="file://$inputs" \
  -e '{"package_cache_paths":[]}'
```

`artifact_source` also accepts an HTTP(S) address serving the bundle's directory layout. See [Preserving Build Dependencies](docs/maintenance.md#preserving-build-dependencies) for creating a bundle.

## Configuration

Settings are grouped by the component they configure:

| Location | Configuration |
| --- | --- |
| [config/void/](config/void/) | Base system, kernel, user account, shell, and optional applications |
| [config/networking/](config/networking/) | DHCP, DNS, and Tailscale |
| [config/cosmic/](config/cosmic/) | Desktop session, audio, and COSMIC preferences |
| [config/intel.yml](config/intel.yml) | Intel graphics |
| [config/nvidia.yml](config/nvidia.yml) | NVIDIA driver, boot policy, and model-gated 550 W limit |
| [config/docker.yml](config/docker.yml) | Docker daemon, service, and NVIDIA container runtime |
| [config/qemu.yml](config/qemu.yml) | Host virtualization settings |
| [config/rustdesk.yml](config/rustdesk.yml) | RustDesk settings and identity restoration |

The playbooks select components by **profile**. [group_vars/all.yml](group_vars/all.yml) maps profile names to configuration files and defines the default selection. To choose a different set, pass a JSON array using the appropriate variable:

| Playbook | Variable | Example |
| --- | --- | --- |
| `bootstrap.yml` | `profiles` | `-e '{"profiles":["base","intel","desktop","docker"]}'` |
| `build/image.yml` | `image_profiles` | `-e '{"image_profiles":["base","intel","desktop","docker"]}'` |
| `tests/vm.yml` | `vm_profiles` | `-e '{"vm_profiles":["base","intel","desktop","docker"]}'` |

Package versions and checksums are stored in [locks/](locks/). Use the [update workflow](docs/maintenance.md#updating-packages) to resolve and test a new set of versions.

Machine addresses and credentials belong in `.env`, using [.env.example](.env.example) as a template. These values are used when [deploying to a machine](docs/maintenance.md#deploying-to-a-machine). Image builds and VM tests do not require the host's credentials.

## Building an Image

After preparing the dependencies, build an image into a new output directory:

```sh
uv run --locked ansible-playbook build/image.yml \
  -e image_directory="$PWD/artifacts/base"
uv run --locked python tests/release.py artifacts/base
```

The playbook installs and configures Void inside a Docker container with networking disabled. It then generates the initramfs and bootloader and exports the installation:

| Output | Contents |
| --- | --- |
| `rootfs.tar.xz` | The configured filesystem, for extraction onto a target installation |
| `root.img` | An ext4 root disk |
| `efi.img` | The UEFI boot disk, used alongside `root.img` |
| `manifest.json` and `source-inputs.json` | Selected profiles and hashes of the configuration used to build the image |
| `packages.json`, `base.json`, `builds.yml`, `collections.json`, and `input-release.json` | Copies of the input locks |
| `SHA256SUMS` | Checksums for the output files |

The image has a writable root filesystem. Account passwords are locked, and machine identities are generated or supplied during deployment. Set up login credentials before using it as a host installation. For a VM with automatic desktop login and SSH access, use the [test image](docs/testing.md#desktop-in-qemu).

Builds require a new output directory each time so an existing image is preserved. Follow [Release Images](docs/testing.md#release-images) to verify the files and boot the image in QEMU.

## License

This project is licensed under the [MIT License](LICENSE).
