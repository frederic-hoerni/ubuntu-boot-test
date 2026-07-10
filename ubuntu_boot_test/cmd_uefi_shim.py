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
      # NOTE AMD64 cloud images use hybrid PC+UEFI setup
      "grub2-common",
      "grub-pc",
      "grub-pc-bin",
      "grub-efi-amd64-bin",
      "grub-efi-amd64-unsigned",
      "grub-efi-amd64-signed",
      "shim-signed"
    )),
    Arch.ARM64: set((
      "grub2-common",
      "grub-efi-arm64",
      "grub-efi-arm64-bin",
      "grub-efi-arm64-unsigned",
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
  #
  # Since shim 16.1, shim-signed ships two shims and selects which one to
  # deploy at runtime based on the Microsoft UEFI CA enrolled in the firmware
  # db (see is-2023-ca-enrolled / setup_alternatives in the shim-signed
  # postinst):
  #   shim<arch>.efi.signed.2023  shim 16.1 (NX), Microsoft UEFI CA 2023
  #   shim<arch>.efi.signed.2011  shim 15.8, Microsoft UEFI CA 2011
  # We test the 16.1 shim: below we enroll the 2023 CA into db so it gets
  # deployed, and target its binary here.
  SHIM_SIGNED_PATH = f"usr/lib/shim/shim{EFI_SUFFIXES[args.arch]}.signed.2023"
  SHIM_FALLBACK_SIGNED_PATH = f"usr/lib/shim/shim{EFI_SUFFIXES[args.arch]}.signed.2011"
  # update-alternatives generic name that resolves to the deployed shim
  SHIM_ALT_PATH = f"/usr/lib/shim/shim{EFI_SUFFIXES[args.arch]}.signed"
  MM_SIGNED_PATH = f"usr/lib/shim/mm{EFI_SUFFIXES[args.arch]}"
  FB_SIGNED_PATH = f"usr/lib/shim/fb{EFI_SUFFIXES[args.arch]}"
  GRUB_SIGNED_PATH = f"usr/lib/grub/{EFI_TARGETS[args.arch]}/grub{EFI_SUFFIXES[args.arch]}.signed"

  # Paths of installed loaders
  SHIM_ESP_PATH = f"/boot/efi/EFI/ubuntu/shim{EFI_SUFFIXES[args.arch]}"
  GRUB_ESP_PATH = f"/boot/efi/EFI/ubuntu/grub{EFI_SUFFIXES[args.arch]}"

  # Prepare packages for install
  package_paths = prepare_packages(TEMPDIR.name, PACKAGE_SETS[args.arch], args.packages)

  # Create virtual machine
  vm = VirtualMachine(TEMPDIR.name, ubuntu_cloud_url(args.release, args.arch), args.arch, Firmware.UEFI)

  def gen_sb_key(guid, name):
    if os.path.isfile(f"{name}.key"):
        return
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
    # Check/sign mm and fb
    for file in (MM_SIGNED_PATH, ):
      binpath = os.path.join(ctx.dir_path, file)
      if not is_canonical_signed(binpath):
        # Sign shim with ephemeral key
        gen_sb_key(SHIM_LOCK_GUID, "MokList")
        sbsign_file("MokList", binpath)

  with deb_repack_ctx(TEMPDIR.name, grub_signed_deb_path) as ctx:
    binpath = os.path.join(ctx.dir_path, GRUB_SIGNED_PATH)
    if not is_canonical_signed(binpath):
      # Sign GRUB with ephemeral key
      gen_sb_key(SHIM_LOCK_GUID, "MokList")
      sbsign_file("MokList", binpath)
    # Save information needed for revocation
    grub_hash_esl = pe_to_efihash(binpath)
    grub_sbat = decode_image_sbat(TEMPDIR.name, binpath)

  # Enroll the Microsoft UEFI CA 2023 into db so that, on install, shim-signed
  # deploys the shim 16.1 binary (shim<arch>.efi.signed.2023).  Keep a copy of
  # db without it so we can also exercise the 15.8 (2011 CA) fallback.
  db_without_2023 = vm.read_efivar(EFI_IMAGE_SECURITY_DATABASE_GUID, "db")
  vm.write_efivar(EFI_IMAGE_SECURITY_DATABASE_GUID, "db",
    uefica_esl("microsoft uefi ca 2023.pem"), append=True)

  def installnew():
    # Copy packages to VM
    vm.copy_files(package_paths, "/tmp/")
    # Install packages
    vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])
    # Install new GRUB
    vm.run_cmd(["grub-install", "/dev/disk/by-id/virtio-0"])
    vm.run_cmd(["update-grub"])
    # With the 2023 CA in db, shim-signed must have selected the 16.1 shim.
    selected = vm.run_cmd(["readlink", "-f", SHIM_ALT_PATH]).strip()
    assert selected.endswith(".signed.2023"), \
      f"expected shim 16.1 (2023) to be deployed, but {SHIM_ALT_PATH} -> {selected}"

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
    vm.waitserial(b"error: bad shim lock signature.") # FIXME: this will change for loader protocol
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

  def mok_disable_validation():
    # Boot as an ephemeral snapshot
    vm.start(ephemeral_snapshot=True)
    # Remove GRUB signature
    with vm.remote_file(GRUB_ESP_PATH) as rf:
      cleansigs(rf.local_path)
    # Run mokutil --disable-validation
    vm.run_cmd(["sh", "-c", "\"printf '11111111\\n11111111\\n' | mokutil --disable-validation\""])
    vm.run_cmd(["sh", "-c", "\"mokutil --timeout -1\""])
    # Reboot and navigate MokManager
    vm.reboot(wait=False)
    def expecthandler(serp):
      # 1. Wait for MokManager
      # 2. Move down to "Change Secure Boot state"
      # 3. Press enter
      serp.expect(b"Perform MOK management")
      serp.send(b"\x1b[B\r\n")
      # Wait for password prompt asking for 3 random chars
      for i in range(3):
        serp.expect(b"Enter password character")
        serp.send(b"1\r\n")
      # Disable Secure Boot screen
      serp.expect(b"Disable Secure Boot")
      serp.send(b"\x1b[B\r\n")
      # Then trigger reboot
      serp.expect(b"Reboot")
      serp.send(b"\r\n")
      # MokManager can hang if you dont read the entire last screen
      serp.read(0x4000)
    vm.expectserial(expecthandler)
    # Wait for successful boot
    vm.waitboot()
    vm.shutdown()

  def mok_import():
    # Boot as an ephemeral snapshot
    vm.start(ephemeral_snapshot=True)
    # Generate key and re-sign GRUB
    pem_priv, pem_cert, esl_cert = gen_efi_signkey()
    with open(os.path.join(TEMPDIR.name, f"MokImport.key"), "wb") as f:
      f.write(pem_priv)
    with open(os.path.join(TEMPDIR.name, f"MokImport.pem"), "wb") as f:
      f.write(pem_cert)
    with open(os.path.join(TEMPDIR.name, f"MokImport.der"), "wb") as f:
      # FIXME: maybe 'gen_efi_signkey()' should return a DER directly
      f.write(esl_cert[44:])
    with vm.remote_file(GRUB_ESP_PATH) as rf:
      cleansigs(rf.local_path)
      sbsign_file("MokImport", rf.local_path)
    # Copy key to machine and run mokutil --disable-validation
    vm.copy_files([os.path.join(TEMPDIR.name, f"MokImport.der")], "/tmp/")
    vm.run_cmd(["sh", "-c", "\"printf '11111111\\n11111111\\n' | mokutil --import /tmp/MokImport.der\""])
    vm.run_cmd(["sh", "-c", "\"mokutil --timeout 111\""])
    # Reboot and navigate MokManager
    vm.reboot(wait=False)
    def expecthandler(serp):
      # Wait for timeout prompt and press space
      serp.expect(b"Booting in 111 seconds")
      serp.send(b" ")
      # Select "Enroll MOK" from menu
      serp.expect(b"Continue boot")
      serp.send(b"\x1b[B\r\n")
      # [Enroll MOK] screen
      serp.expect(b"[Enroll MOK]")
      serp.send(b"\x1b[B\r\n")
      # "Enroll the key(s)?" screen
      serp.expect(b"Enroll the key(s)?")
      serp.send(b"\x1b[B\r\n")
      # "Password:" prompt
      serp.expect(b"Password:")
      serp.send(b"11111111\r\n")
      # Trigger reboot
      serp.expect(b"Reboot")
      serp.send(b"\r\n")
      # MokManager can hang if you dont read the entire last screen
      serp.read(0x4000)
    vm.expectserial(expecthandler)
    # Wait for successful boot
    vm.waitboot()
    vm.shutdown()

  def mok_sbat_policy():
    # Boot as an ephemeral snapshot
    vm.start(ephemeral_snapshot=True)
    # Get current revocations
    automatic_revocations = vm.run_cmd(["cat", "/sys/firmware/efi/efivars/SbatLevelRT-605dab50-e046-4300-abb6-3dd810dd8b23"])
    # Ask mokutil to upgrade to "latest"
    vm.run_cmd(["mokutil", "--set-sbat-policy", "latest"])
    vm.reboot()
    # Assert that they actually updated
    latest_revocations = vm.run_cmd(["cat", "/sys/firmware/efi/efivars/SbatLevelRT-605dab50-e046-4300-abb6-3dd810dd8b23"])
    assert automatic_revocations != latest_revocations
    # Shutdown
    vm.shutdown()

  def shim_ca_fallback():
    # When the Microsoft UEFI CA 2023 is not enrolled in db, shim-signed must
    # fall back to the 15.8 (2011 CA) shim.  Remove the 2023 CA, re-run the
    # selection (dpkg-reconfigure) in an ephemeral snapshot, and check that
    # the alternative now resolves to the 2011 shim.
    vm.write_efivar(EFI_IMAGE_SECURITY_DATABASE_GUID, "db", db_without_2023, append=False)
    vm.start(ephemeral_snapshot=True)
    vm.run_cmd(["sh", "-c", "\"DEBIAN_FRONTEND=noninteractive dpkg-reconfigure shim-signed\""])
    selected = vm.run_cmd(["readlink", "-f", SHIM_ALT_PATH]).strip()
    vm.forceshutdown()
    # Restore db (re-enroll the 2023 CA) before asserting.
    vm.write_efivar(EFI_IMAGE_SECURITY_DATABASE_GUID, "db",
      uefica_esl("microsoft uefi ca 2023.pem"), append=True)
    assert selected.endswith(".signed.2011"), \
      f"expected shim 15.8 (2011) fallback, but {SHIM_ALT_PATH} -> {selected}"

  TASKS = [
    (lambda: True,
      vm.start,               "Boot and provision image"),
    (lambda: True,
      installnew,             "Install new bootloaders"),
    (lambda: True,
      vm.shutdown,            "Shut down virtual machine"),
    (lambda: True,
      vm.start,               "Boot with new bootloaders"),
    (lambda: True,
      vm.shutdown,            "Shut down virtual machine"),
    (lambda: True,
      shim_ca_fallback,       "Secure Boot CA: 15.8 fallback when 2023 CA absent"),
    (lambda: True,
      strip_shim,             "Secure Boot: stripping shim signature"),
    (lambda: True,
      strip_grub,             "Secure Boot: stripping GRUB signature"),
    (lambda: True,
      strip_kernel,           "Secure Boot: stripping kernel signature"),
    (lambda: True,
      revoke_shim_dbx,        "Secure Boot: revoking shim via dbx"),
    (lambda: True,
      revoke_grub_dbx,        "Secure Boot: revoking GRUB via dbx"),
    (lambda: True,
      revoke_grub_mlx,        "Secure Boot: revoking GRUB via MokListX"),
    (lambda: True,
      revoke_shim_sbat,       "Secure Boot: revoking shim via SBAT"),
    (lambda: True,
      revoke_grub_sbat,       "Secure Boot: revoking GRUB via SBAT"),
    (lambda: True,
      mok_disable_validation, "MOK: Verifying --disable-validation works"),
    (lambda: True,
      mok_import,             "MOK: Verifying --import works"),
    (lambda: True,
      mok_sbat_policy,        "MOK: Verifying --set-sbat-policy works"),
  ]

  for predicate, do_task, msg in TASKS:
    if predicate():
      do_task()
      print(f"{msg} OK")
