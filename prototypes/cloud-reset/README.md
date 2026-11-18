# Omarchy connected reset prototype

The proposed **Reset Computer → Download and reinstall latest stable** option would give an upgraded PC the same software and defaults as a clean install from the selected release’s USB. This x86-64 prototype provides a command-line builder, a one-time UEFI boot command for a disposable guest, and tests for the resulting installation.

## Try it in a disposable VM

On an x86 Omarchy machine with QEMU and OVMF, run the following as an ordinary user from this directory.

```sh
bin/test-install --version 4.5.0
```

The test uses one temporary 64 GiB sparse disk, private UEFI variables and a wired virtual network. It installs the official release as a reference, boots it, records installed package versions and selected user defaults, and makes identifiable changes to both the system and the user’s settings. It then arms the recovery image through the guest’s UEFI `BootNext` and starts the same VM again without the installer ISO attached. The guest has to download the full ISO over its virtual network. The final boot must come from the installed disk, with the test changes removed and the packages and sampled defaults matching the reference.

The harness provisions a temporary test user on an unencrypted guest using the installer’s unattended mode. It compares two installations of the same release and serves the verified ISO from this computer for a repeatable test. OEM handoff, 4.0-to-4.5 upgrades, LUKS and dual-boot installations need separate acceptance tests.

The shorter `bin/test-vm --version 4.5.0` exercises the firmware boot and validates the installation medium from live userspace. Both commands accept `--iso /path/to/omarchy-4.5.0-x86.iso` with its adjacent `.sig` for an offline host. Omit the version and ISO to fetch the latest stable release.

### Requirements

The host needs the Omarchy release keyring, Python 3.11+, GnuPG, curl, bsdtar, binutils, mkinitcpio, the x64 systemd EFI stub, OpenSSL, OpenSSH and the x86 QEMU system emulator with OVMF. On Arch the emulator and firmware packages are `qemu-system-x86` and `edk2-ovmf`. The guest uses RAM equal to the ISO size rounded up to a GiB plus 4 GiB. Leave space on the host for the cached ISO and a populated guest disk. The full test accepts `--work-dir` to choose where the temporary VM files live.

## Build the recovery application

```sh
bin/omarchy-cloud-reset --version 4.5.0 --output "$PWD/omarchy-recovery.efi"
bootctl kernel-inspect "$PWD/omarchy-recovery.efi"
```

The builder verifies the release checksum and detached signature, extracts the installer kernel and initramfs, and packages a Unified Kernel Image (UKI). A local ISO supplied with `--iso` is checked using its detached signature. The UKI contains the release URL, byte count and SHA-256 digest. The recovery environment fetches the ISO into RAM over wired DHCP, verifies it and loop-mounts it read-only. Keeping the complete ISO available lets the existing installer access both its live environment and the separate system image while it replaces the guest installation.

In the prototype, preparing an image from the internet downloads the ISO to obtain the trusted boot components. Booting that image from its official release URL downloads the ISO again. The VM tests cache and serve the same verified release over loopback.

Inside an **already installed, marked test guest** with the project available, `bin/omarchy-cloud-reset --schedule` prepares the latest stable release and selects it for the guest’s next reboot. The scheduler checks the QEMU guest identity, the marked target disk and its writable EFI partition. It creates a one-time firmware entry and preserves the existing boot order. Before reboot, `sudo bin/schedule-recovery --cancel` removes that transaction. The automated full test sets up the guest and exercises the scheduler. There is no desktop menu integration or physical-machine scheduling in this prototype.

## Checks exercised here

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
/usr/lib/initcpio/busybox ash tests/test-hook
```

The offline tests exercise signature verification, ISO9660 extraction, recovery-image construction, one-time boot selection and rollback, and the installer test fixture. One test uses the packaged Omarchy kernel and a fixture initramfs to assemble a real EFI executable, identify it with `bootctl` and read its recovery hook and pinned media metadata back from the embedded initramfs. Firmware mutations and network/mount operations are simulated in the corresponding component tests.

This development environment lacks the system emulator, OVMF and the release ISO, and restricts network access. A complete UEFI boot and disk reinstall have not been observed here.

[Feature discussion](https://github.com/omacom/omarchy/discussions/19318) · [Upstream Omarchy installer](https://github.com/omacom/omarchy-iso)
