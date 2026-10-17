# Maintenance

Package updates are prepared separately from the versions used for installation. The updater resolves a proposed set of packages, downloads their archives, and writes new lockfiles for review. You can build and test that proposal in QEMU before accepting it or changing an existing machine.

## Updating Packages

Complete the [project setup](../README.md#getting-started) first. Run the updater from the repository root:

```sh
uv run --locked ansible-playbook build/update.yml
```

It checks the package templates, refreshes their build dependencies, and builds recipes affected by those changes. XBPS then resolves the packages selected by the configuration and downloads their dependencies. The updater prints a directory under `.work/updates/` containing the **candidate**: the proposed installation and the files needed to test it.

| Candidate file | Contents |
| --- | --- |
| `packages.json` and `packages/` | Proposed package lock and verified installation archives |
| `builds.yml` | Proposed source revisions and build-tool pins |
| `source-changes.json` | Changes to package templates and shared build files |
| `tool-changes.json` | Changes to the tools required by those templates |
| `changes.json` | Package additions, removals, and version changes |
| `configuration-changes.json` | Changes to COSMIC's packaged configuration defaults |

The updater leaves `locks/` unchanged. The NVIDIA profile retains `nvidia*` at the accepted versions during ordinary updates. Set `update_keep` explicitly when changing the driver set.

### Testing and Accepting an Update

Stop any running test VM before rebuilding it. Set `candidate` to the directory printed by the updater:

```sh
candidate=/absolute/path/to/candidate
uv run --locked ansible-playbook tests/vm.yml \
  -e package_lock_file="$candidate/packages.json" \
  -e package_input_directory="$candidate/packages"
uv run --locked python tests/qemu.py start --fresh --record-audio
```

Once the desktop has loaded, check that COSMIC accepts the configured preferences:

```sh
uv run --locked python tests/desktop.py --candidate "$candidate"
```

The check uses the guest's installed COSMIC applications to read the preferences. It also checks whether the update removed any packaged configuration keys or schema directories referenced by those preferences. Inspect the desktop and try the components affected by the upgrade to check their appearance and behavior.

Shut down the guest after testing:

```sh
uv run --locked python tests/qemu.py stop
```

Wait for `.work/qemu/qemu.pid` to disappear. After reviewing the change reports and test results, accept the candidate and package its verified archives:

```sh
cp "$candidate/packages.json" locks/packages.json
cp "$candidate/builds.yml" locks/builds.yml
tag=$(uv run --locked python scripts/inputs.py tag)
bundle="artifacts/$tag-bundle"
uv run --locked python scripts/inputs.py create "$bundle" --packages "$candidate/packages"
uv run --locked python scripts/inputs.py pack "$bundle" "artifacts/$tag" \
  --source "https://github.com/Qonfused/wdc-server/releases/download/$tag"
cp "artifacts/$tag/input-release.json" locks/input-release.json
uv run --locked python scripts/inputs.py check locks/input-release.json --project .
uv run --locked ansible-playbook prepare.yml -e artifact_source="file://$PWD/$bundle"
```

Review and commit the three changed locks together, then publish the prepared assets as described under [Preserving Build Dependencies](#preserving-build-dependencies). Future builds will use the accepted versions. Applying them to an existing machine is a separate [deployment](#deploying-to-a-machine). Removing a package from the lock does not uninstall it from that machine.

### Checking for Changes Without Building

To check package templates without compiling packages or resolving an installation:

```sh
uv run --locked ansible-playbook build/update.yml -e update_check_only=true
```

Use `update_tools_only=true` to also resolve updated build dependencies. Both modes stop after writing their proposal. The weekly [GitHub Actions check](../.github/workflows/check-updates.yml) compares templates and saves its source report and proposed `builds.yml` as workflow artifacts.

The full update supports these overrides:

| Variable | Purpose |
| --- | --- |
| `update_builds` | Select which external recipes to check |
| `update_profiles` | Select which configuration profiles to resolve |
| `update_keep` | Retain packages matching the supplied patterns at their accepted versions |
| `update_extra_packages` | Request additional packages during resolution |
| `update_source_proposal` | Use a directory from an earlier source check |
| `update_candidates` | Reuse named package builds after checking their source and tool pins |

Each `update_keep` pattern must match at least one package in the accepted lock.

### Building an Individual Package

Package recipes are selected in [locks/builds.yml](../locks/builds.yml). Chrome uses Void's `google-chrome` template, RustDesk uses a template overlay, and COSMIC uses a pinned Void pull request. The lock records the source revisions and build tools for each recipe.

To build a single recipe:

```sh
uv run --locked ansible-playbook build/package.yml -e package=google-chrome
```

The resulting archives and `candidate.json` are written under `.work/package-builds/google-chrome/`. This command uses the declared build environment and lets the native template download its sources. It does not change the installation lock. The [Chrome test](testing.md#chrome) checks that the built package runs with the installation's libraries.

## Lockfiles

| File | What it pins |
| --- | --- |
| [locks/packages.json](../locks/packages.json) | Installed package versions, architectures, archive checksums, and the kernel series |
| [locks/builds.yml](../locks/builds.yml) | Package source revisions, build image digests, tools, and recipe selections |
| [locks/base.json](../locks/base.json) | The initial Void rootfs |
| [locks/collections.json](../locks/collections.json) | Ansible collections and their dependencies |
| [locks/input-release.json](../locks/input-release.json) | Download locations, sizes, and checksums of the installation input release |
| [uv.lock](../uv.lock) | Python dependencies for running the playbooks and tools |

The `kernel_package` field in `packages.json` selects the kernel series. The NVIDIA profile derives the header package name by appending `-headers` and checks that both packages have the same release. Bootstrap also requires matching NVIDIA userspace and kernel-module versions.

The locks describe the installation inputs and package build environments. Image assembly uses Docker, XBPS, and mtools installed on the build machine. Reproducing the selected packages does not guarantee byte-identical disk images across different build machines.

## Preserving Build Dependencies

An **input bundle** saves the exact archives needed to install a locked version of the system. Keeping it alongside an image allows a fresh checkout to rebuild that installation if the package cache is lost or upstream removes an archive. The accepted-update steps above already create the bundle and release parts for a new package lock.

After running `prepare.yml`, create a bundle in a new directory:

```sh
uv run --locked python scripts/inputs.py create artifacts/inputs
uv run --locked python scripts/inputs.py verify artifacts/inputs --project .
```

The bundle contains the Void rootfs, XBPS package archives, Ansible collections, and input locks. Verification checks every file against its declared checksum and compares the locks with the checkout. It rejects missing, changed, or unlisted files.

Split the bundle into release assets and record their checksums:

```sh
tag=$(uv run --locked python scripts/inputs.py tag)
uv run --locked python scripts/inputs.py pack artifacts/inputs "artifacts/$tag" \
  --source "https://github.com/Qonfused/wdc-server/releases/download/$tag"
cp "artifacts/$tag/input-release.json" locks/input-release.json
```

After committing and pushing the matching locks, publish the tar files:

```sh
gh release create "$tag" "artifacts/$tag"/inputs-*.tar \
  --title "$tag" --notes "Locked installation inputs for $tag"
```

The release assets are installation inputs; build caches and workflow artifacts may keep additional copies. Run `prepare.yml` from a fresh checkout to verify that the published assets restore the locked installation. The alternative local-bundle path is in [Preparing Dependencies](../README.md#preparing-dependencies).

These are binary installation dependencies. Recompiling a package also requires its sources and build tools, which the bundle does not collect. Python downloads for the machine running Ansible are described separately by `uv.lock`.

To bundle an unaccepted package update, supply `--lock /path/to/packages.json` and `--packages /path/to/packages` to `create`. Pass the same `--lock` with `--project` when verifying it.

## Deploying to a Machine

`bootstrap.yml` applies the selected profiles to an existing Void x86_64 glibc installation over SSH or to a mounted filesystem through a chroot. It requires the prepared package repository to be available at `/opt/wdc-packages` inside the target. For a new installation, prepare the filesystem and mounts using Void's [chroot installation guide](https://docs.voidlinux.org/installation/guides/chroot.html).

Create the private deployment settings:

```sh
cp .env.example .env
chmod 600 .env
```

Set the target and account values in `.env`. The file is excluded from Git and must be loaded explicitly with `uv run --env-file .env`.

### Over SSH

Set `WDC_TARGET` to the machine's address, `WDC_CONNECTION=ssh`, and `WDC_SSH_USER` to an account with sudo access. Use normal SSH authentication, then run:

```sh
uv run --env-file .env --locked ansible-playbook bootstrap.yml \
  -e target_running=true
```

`target_running=true` applies service changes immediately. Set `generate_boot_files=true` when boot configuration must also be regenerated; `generate_initramfs` can be set independently when only the initramfs needs rebuilding.

### Into a Mounted Filesystem

Set `WDC_TARGET` to the mounted root's absolute path and `WDC_CONNECTION=community.general.chroot`. Run with permission to enter it:

```sh
sudo uv run --env-file .env --locked ansible-playbook bootstrap.yml
```

Complete the bootloader setup and account credentials before booting a new installation. After login, start COSMIC with `~/.local/bin/start-desktop`.

### Remote Access and Machine Identity

Enroll Tailscale on the running machine with:

```sh
sudo tailscale up --accept-dns=false
```

This preserves the DNS policy configured through openresolv. Docker starts independently of Tailscale enrollment.

To restore an existing RustDesk identity, populate its private values in `.env` and set `WDC_RUSTDESK_RESTORE_IDENTITY=true`. Bootstrap writes missing identity files and preserves existing ones. Base images and VM tests disable this restore step so they can be created without the host's identity.

When replacing a system image, retain the machine's `/home`, Docker data, Tailscale and RustDesk state, SSH host keys, and machine IDs. Plan where that data will live before replacing a running system. The image builder produces the system filesystem, with disk partitioning and data migration left to deployment.
