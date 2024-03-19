# ubuntu-boot-test: cmd_uefi_shim.py: UEFI shim boot test
#
# Copyright (C) 2023 Canonical, Ltd.
# Author: Mate Kukri <mate.kukri@canonical.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from ubuntu_boot_test.config import *
from ubuntu_boot_test.util import *
from ubuntu_boot_test.vm import VirtualMachine
import os
import subprocess
import tempfile

def register(subparsers):
  parser = subparsers.add_parser("uefi_shim",
    description="UEFI shim boot test")

  parser.add_argument("-r", "--release", required=True,
    help="Guest Ubuntu release")
  parser.add_argument("-a", "--arch", required=True, type=Arch,
    help="Guest architecture")
  parser.add_argument("packages", nargs="*",
    help="List of packages to install (instead of apt-get download)")

def execute(args):
  TEMPDIR = tempfile.TemporaryDirectory("")

  PACKAGE_SETS = {
    Arch.AMD64: set((
      "grub2-common",
      "grub-common",
      "grub-efi-amd64",
      "grub-efi-amd64-bin",
      "grub-efi-amd64-signed",
      "shim-signed"
    )),
    Arch.ARM64: set((
      "grub2-common",
      "grub-common",
      "grub-efi-arm64",
      "grub-efi-arm64-bin",
      "grub-efi-arm64-signed",
      "shim-signed"
    )),
  }

  EFI_SUFFIXES = {
    Arch.AMD64: "x64.efi",
    Arch.ARM64: "aa64.efi"
  }

  EFI_TARGETS = {
    Arch.AMD64: "x86_64-efi-signed",
    Arch.ARM64: "arm64-efi-signed"
  }

  # Paths of packaged loaders
  SHIM_SIGNED_PATH = f"usr/lib/shim/shim{EFI_SUFFIXES[args.arch]}.signed.latest"
  GRUB_SIGNED_PATH = f"usr/lib/grub/{EFI_TARGETS[args.arch]}/grub{EFI_SUFFIXES[args.arch]}.signed"

  # Paths of installed loaders
  SHIM_ESP_PATH = f"/boot/efi/EFI/ubuntu/shim{EFI_SUFFIXES[args.arch]}"
  GRUB_ESP_PATH = f"/boot/efi/EFI/ubuntu/grub{EFI_SUFFIXES[args.arch]}"

  # Prepare packages for install
  package_paths = prepare_packages(TEMPDIR.name, PACKAGE_SETS[args.arch], args.packages)

  # Create virtual machine
  vm = VirtualMachine(TEMPDIR.name, ubuntu_cloud_url(args.release, args.arch), args.arch, Firmware.UEFI)

  def gen_sb_key(guid, name):
    pem_priv, pem_cert, esl_cert = gen_efi_signkey()
    with open(os.path.join(TEMPDIR.name, f"{name}.key"), "wb") as f:
      f.write(pem_priv)
    with open(os.path.join(TEMPDIR.name, f"{name}.pem"), "wb") as f:
      f.write(pem_cert)
    vm.write_efivar(guid, name, esl_cert, append=True)

  def sbsign_file(with_key, path):
    result = subprocess.run(["sbsign",
      "--key", os.path.join(TEMPDIR.name, f"{with_key}.key"),
      "--cert", os.path.join(TEMPDIR.name, f"{with_key}.pem"),
      "--output", path, path], capture_output=not DEBUG)
    assert result.returncode == 0, f"Failed to sign {path}"

  shim_deb_path = None
  grub_signed_deb_path = None
  for package_path in package_paths:
    if "shim-signed" in package_path:
      shim_deb_path = package_path
    if "grub-efi-" in package_path and "-signed" in package_path:
      grub_signed_deb_path = package_path

  with deb_repack_ctx(TEMPDIR.name, shim_deb_path) as ctx:
    binpath = os.path.join(ctx.dir_path, SHIM_SIGNED_PATH)
    if not is_uefica_signed(binpath):
      # Sign shim with ephemeral key
      gen_sb_key(EFI_IMAGE_SECURITY_DATABASE_GUID, "db")
      sbsign_file("db", binpath)
    # Save information needed for revocation
    shim_hash_esl = pe_to_efihash(binpath)
    shim_sbat = decode_image_sbat(TEMPDIR.name, binpath)

  with deb_repack_ctx(TEMPDIR.name, grub_signed_deb_path) as ctx:
    binpath = os.path.join(ctx.dir_path, GRUB_SIGNED_PATH)
    if not is_canonical_signed(binpath):
      # Sign GRUB with ephemeral key
      gen_sb_key(SHIM_LOCK_GUID, "MokList")
      sbsign_file("MokList", binpath)
    # Save information needed for revocation
    grub_hash_esl = pe_to_efihash(binpath)
    grub_sbat = decode_image_sbat(TEMPDIR.name, binpath)

  def installnew():
    # Copy packages to VM
    vm.copy_files(package_paths, "/tmp/")
    # Install packages
    vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])
    # Install new GRUB
    vm.run_cmd(["grub-install", "/dev/disk/by-id/virtio-0"])
    vm.run_cmd(["update-grub"])

  def cleansigs(path):
    with maybe_gzip_ctx(path) as ctx:
      # Remove exisiting signatures from the binary
      while subprocess.run(["sbattach", "--remove", ctx.path],
                           capture_output=not DEBUG).returncode == 0:
        pass

  def strip_shim():
    # Boot as an ephemeral snapshot
    vm.start(ephemeral_snapshot=True)
    # Clean signatures
    with vm.remote_file(SHIM_ESP_PATH) as rf:
      cleansigs(rf.local_path)
    # Reboot and verify that we get "Access denied"
    vm.reboot(wait=False)
    vm.waitserial(f"shim{EFI_SUFFIXES[args.arch]}: Access Denied".encode())
    # Force shutdown VM
    vm.forceshutdown()

  def strip_grub():
    # Boot as an ephemeral snapshot
    vm.start(ephemeral_snapshot=True)
    # Clean signatures
    with vm.remote_file(GRUB_ESP_PATH) as rf:
      cleansigs(rf.local_path)
    # Reboot and wait for error from MokManager
    vm.reboot(wait=False)
    vm.waitserial(b"Verification failed: (0x1A) Security Violation")
    # Force shutdown VM
    vm.forceshutdown()

  def strip_kernel():
    # Boot as an ephemeral snapshot
    vm.start(ephemeral_snapshot=True)
    # Find all kernels
    kernel_paths = vm.run_cmd(["find", "/boot", "-name", "vmlinuz-*"]).splitlines()
    if DEBUG:
      print(f"Found kernels: {kernel_paths}")
    # Clean signatures
    for kernel_path in kernel_paths:
      with vm.remote_file(kernel_path) as rf:
        cleansigs(rf.local_path)
    # Reboot and wait for error from GRUB
    vm.reboot(wait=False)
    vm.waitserial(b"error: bad shim signature.")
    # Force shutdown VM
    vm.forceshutdown()

  def revoke_hash_esl(varguid, varname, hash_esl, failmsg):
    bk = vm.read_efivar(varguid, varname)
    # Add binary hash to revocation list
    vm.write_efivar(varguid, varname, hash_esl, append=True)
    # Ensure it is revoked
    vm.start(ephemeral_snapshot=True, wait=False)
    vm.waitserial(failmsg)
    vm.forceshutdown()
    # Restore
    vm.write_efivar(varguid, varname, bk, append=False)

  def revoke_shim_dbx():
    revoke_hash_esl(EFI_IMAGE_SECURITY_DATABASE_GUID, "dbx", shim_hash_esl,
      f"shim{EFI_SUFFIXES[args.arch]}: Access Denied".encode())

  def revoke_grub_dbx():
    revoke_hash_esl(EFI_IMAGE_SECURITY_DATABASE_GUID, "dbx", grub_hash_esl,
      b"Verification failed: (0x1A) Security Violation")

  def revoke_grub_mlx():
    revoke_hash_esl(SHIM_LOCK_GUID, "MokListX", grub_hash_esl,
      b"Verification failed: (0x1A) Security Violation")

  def revoke_comp_sbat(cnam, cgen, failmsg):
    bk = vm.read_efivar(SHIM_LOCK_GUID, "SbatLevel")
    # Set SbatLevel for component
    sbat_level = SbatLevel(bk)
    sbat_level.set_level_for(cnam, cgen)
    vm.write_efivar(SHIM_LOCK_GUID, "SbatLevel", sbat_level.encode(), append=False)
    # Ensure it is revoked
    vm.start(ephemeral_snapshot=True, wait=False)
    vm.waitserial(failmsg)
    vm.forceshutdown()
    # Restore
    vm.write_efivar(SHIM_LOCK_GUID, "SbatLevel", bk, append=False)

  def revoke_shim_sbat():
    revoke_comp_sbat("shim", shim_sbat.get_level_for("shim") + 1,
      b"Verifiying shim SBAT data failed: Security Policy Violation")

  def revoke_grub_sbat():
    revoke_comp_sbat("grub", grub_sbat.get_level_for("grub") + 1,
      b"Verification failed: (0x1A) Security Violation")

  TASKS = [
    (lambda: True,
      vm.start,         "Boot and provision image"),
    (lambda: True,
      installnew,       "Install new bootloaders"),
    (lambda: True,
      vm.shutdown,      "Shut down virtual machine"),
    (lambda: True,
      vm.start,         "Boot with new bootloaders"),
    (lambda: True,
      vm.shutdown,      "Shut down virtual machine"),
    (lambda: True,
      strip_shim,       "Secure Boot: stripping shim signature"),
    (lambda: True,
      strip_grub,       "Secure Boot: stripping GRUB signature"),
    (lambda: True,
      strip_kernel,     "Secure Boot: stripping kernel signature"),
    (lambda: True,
      revoke_shim_dbx,  "Secure Boot: revoking shim via dbx"),
    (lambda: True,
      revoke_grub_dbx,  "Secure Boot: revoking GRUB via dbx"),
    (lambda: True,
      revoke_grub_mlx,  "Secure Boot: revoking GRUB via MokListX"),
    (lambda: True,
      revoke_shim_sbat, "Secure Boot: revoking shim via SBAT"),
    (lambda: True,
      revoke_grub_sbat, "Secure Boot: revoking GRUB via SBAT"),
  ]

  for predicate, do_task, msg in TASKS:
    if predicate():
      do_task()
      print(f"{msg} OK")
