# ubuntu-boot-test: cmd_uefi_mass.py: MAAS style netboot test (UEFI)
#
# Copyright (C) 2024 Canonical, Ltd.
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
from ubuntu_boot_test.net import VirtualNetwork
from ubuntu_boot_test.util import *
from ubuntu_boot_test.vm import VirtualMachine
import json
import os
import shutil
import subprocess
import tempfile

def register(subparsers):
  parser = subparsers.add_parser("uefi_maas",
    description="MAAS style netboot test (UEFI)")

  parser.add_argument("-r", "--release", required=True,
    help="Guest Ubuntu release")
  parser.add_argument("-a", "--arch", required=True, type=Arch,
    help="Guest architecture")
  parser.add_argument("packages", nargs="*",
    help="List of packages to install (instead of apt-get download)")

def execute(args):
  TEMPDIR = tempfile.TemporaryDirectory("")

  MAAS_IMAGES_URL = "http://images.maas.io/ephemeral-v3/stable"
  MAAS_STABLE_STREAM_URL = f"{MAAS_IMAGES_URL}/streams/v1/com.ubuntu.maas:stable:1:bootloader-download.json"

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
  GRUBNET_SIGNED_PATH = f"usr/lib/grub/{EFI_TARGETS[args.arch]}/grubnet{EFI_SUFFIXES[args.arch]}.signed"

  # Download MAAS stable loader
  def fetch_maas_loaders():
    stream = json.loads(fetch_file(MAAS_STABLE_STREAM_URL))
    product = {
                Arch.ARM64: "com.ubuntu.maas.stable:1:grub-efi:uefi:arm64",
                Arch.AMD64: "com.ubuntu.maas.stable:1:grub-efi-signed:uefi:amd64"
              }
    products = stream["products"][product[args.arch]]
    versions = max(products["versions"].items(), key=lambda x: x[0])[1]
    download_file(f"{MAAS_IMAGES_URL}/" + versions["items"]["shim-signed"]["path"],
                  os.path.join(TEMPDIR.name, "maas-shim-signed.tar.xz"))
    runcmd(["tar", "--transform=s/^boot/maas-shim/", "-xf",
            os.path.join(TEMPDIR.name, "maas-shim-signed.tar.xz")],
      cwd=TEMPDIR.name)
    download_file(f"{MAAS_IMAGES_URL}/" + versions["items"]["grub2-signed"]["path"],
                  os.path.join(TEMPDIR.name, "maas-grub-signed.tar.xz"))
    runcmd(["tar", "--transform=s/^grub/maas-grub/", "-xf",
            os.path.join(TEMPDIR.name, "maas-grub-signed.tar.xz")],
      cwd=TEMPDIR.name)

  fetch_maas_loaders()

  # Netboot directory
  netbootdir = os.path.join(TEMPDIR.name, "netboot")
  os.mkdir(netbootdir)
  os.mkdir(os.path.join(netbootdir, "grub"))

  # Write "grub.cfg"
  # Ubuntu loader lives at /EFI/ubuntu/shimx64.efi
  # openSUSE loader is (only) at /EFI/ubuntu/shimx64.efi
  # (We cannot boot it with chainloader directly because signature verification
  #  is only done via shim in our grub when using chainloader)
  NETBOOT_GRUBCFG = f"""\
BOOTLOADER=/efi/ubuntu/shim{EFI_SUFFIXES[args.arch]}
search --set=root --file $BOOTLOADER
if [ $? -eq 0 ]; then
  chainloader $BOOTLOADER
  boot
fi
BOOTLOADER=/efi/boot/boot{EFI_SUFFIXES[args.arch]}
search --set=root --file $BOOTLOADER
if [ $? -eq 0 ]; then
  rmmod peimage
  exit
fi
"""
  with open(os.path.join(netbootdir, "grub", "grub.cfg"), "w") as f:
    f.write(NETBOOT_GRUBCFG)

  # Setup netboot environment
  vnet = VirtualNetwork(TEMPDIR.name, netbootdir)
  tap = vnet.new_tap()

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
    # Copy shim to netboot directory
    shutil.copy(binpath, os.path.join(netbootdir, f"boot{EFI_SUFFIXES[args.arch]}"))

  with deb_repack_ctx(TEMPDIR.name, grub_signed_deb_path) as ctx:
    binpath = os.path.join(ctx.dir_path, GRUB_SIGNED_PATH)
    netbinpath = os.path.join(ctx.dir_path, GRUBNET_SIGNED_PATH)
    if not is_canonical_signed(binpath):
      # Sign GRUB with ephemeral key
      gen_sb_key(SHIM_LOCK_GUID, "MokList")
      sbsign_file("MokList", binpath)
      sbsign_file("MokList", netbinpath)
    # Copy GRUB to netboot directory
    shutil.copy(netbinpath, os.path.join(netbootdir, f"grub{EFI_SUFFIXES[args.arch]}"))

  def setup_netboot_type(netboot_type):
    # Iterate BootXXXX entries and disable entries we don't want
    boot_order = vm.read_efivar(EFI_GLOBAL_VARIABLE_GUID, "BootOrder")
    for i in range(0, len(boot_order), 2):
      num = int(boot_order[i+1])<<8|int(boot_order[i])
      val = bytearray(vm.read_efivar(EFI_GLOBAL_VARIABLE_GUID, f"Boot{num:04X}"))
      name = []
      for j in range(6, len(val), 2):
        if val[j] == 0:
          break
        name.append(chr(int(val[j+1]<<8|int(val[j]))))
      name = "".join(name)
      # We identify the card we want to netboot from via this hard-coded MAC,
      # it is kind of ugly, but parsing device paths is more annoying
      if netboot_type not in name or "(MAC:000000000001)" not in name:
        # Bit 0 of Attributes is LOAD_OPTION_ACTIVE
        val[0] &= 0xfe
        vm.write_efivar(EFI_GLOBAL_VARIABLE_GUID, f"Boot{num:04X}", val, append=False)

  def netboot(standard):
    setup_netboot_type(standard)
    vm.start(tapname=tap, wait=False)
    # Make sure we really are netbooting by "grep"-ing for output
    # It would be nice if we could just disable all other BootXXXX entries
    # but OVMF still tries built-in entries even when setting ACTIVE=0
    if "HTTP" in standard:
      # Shim doesn't print anything on HTTP boot, but firmware prints this
      vm.waitserial(b"URI: http://")
    else:
      # This is printed by shim on TFTP boot
      vm.waitserial(b"Fetching Netboot Image")
    vm.waitboot()
    vm.shutdown()

  def deploymaas():
    shutil.copy(
      os.path.join(TEMPDIR.name, f"maas-shim{EFI_SUFFIXES[args.arch]}"),
      os.path.join(netbootdir, f"boot{EFI_SUFFIXES[args.arch]}"))
    shutil.copy(
      os.path.join(TEMPDIR.name, f"maas-grub{EFI_SUFFIXES[args.arch]}"),
      os.path.join(netbootdir, f"grub{EFI_SUFFIXES[args.arch]}"))

  def installnew():
    # Boot VM
    vm.start(tapname=tap)
    # Install new packages
    vm.copy_files(package_paths, "/tmp/")
    vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])
    vm.run_cmd(["grub-install", "/dev/disk/by-id/virtio-0"])
    vm.run_cmd(["update-grub"])
    # Shutdown
    vm.shutdown()

  def provision_opensuse():
    vm.replace_image(opensuse_cloud_url(args.arch))
    vm.start()
    vm.shutdown()

  TASKS = [
    (lambda: True,
      vm.start,                      "Boot and provision image"),
    (lambda: True,
      vm.shutdown,                   "Shut down virtual machine"),

    # Tested -> Distro
    (lambda: True,
      lambda: netboot("PXEv4"),      "PXEv4: Tested->Distro chainload"),
    (lambda: True,
      lambda: netboot("PXEv6"),      "PXEv6: Tested->Distro chainload"),
    (lambda: True,
      lambda: netboot("HTTPv4"),     "HTTPv4: Tested->Distro chainload"),
    # IPv6 HTTP boot currently only works when manually selected from the boot
    # menu, but not via "BootOrder". This seems like an edk2 bug.
    # (lambda: True,
    #   lambda: netboot("HTTPv6"),     "HTTPv6: Tested->Distro chainload"),

    # MAAS -> Tested chainload
    (lambda: True,
      deploymaas,                    "Deploy MAAS bootloaders to server"),
    (lambda: True,
      installnew,                    "Deploy tested bootloaders to image"),
    (lambda: True,
      lambda: netboot("PXEv4"),      "PXEv4: MAAS->Tested chainload"),
    (lambda: True,
      lambda: netboot("PXEv6"),      "PXEv6: MAAS->Tested chainload"),
    (lambda: True,
      lambda: netboot("HTTPv4"),     "HTTPv4: MAAS->Tested chainload"),
    # (lambda: True,
    #   lambda: netboot("HTTPv6"),   "HTTPv6: MAAS->Tested chainload"),

    # MAAS -> openSUSE chainload
    # FIXME: stop disabling Secure Boot after openSUSE rolls out a new shim
    (lambda: args.arch == Arch.AMD64,
      vm.disablesb,                  "Disable Secure Boot"),
    (lambda: args.arch == Arch.AMD64,
      provision_opensuse,            "Provision openSUSE image"),
    (lambda: args.arch == Arch.AMD64,
      lambda: netboot("PXEv4"),      "PXEv4: MAAS->openSUSE chainload"),
    (lambda: args.arch == Arch.AMD64,
      lambda: netboot("PXEv6"),      "PXEv6: MAAS->openSUSE chainload"),
    (lambda: args.arch == Arch.AMD64,
      lambda: netboot("HTTPv4"),     "HTTPv4: MAAS->openSUSE chainload"),
    # (lambda: args.arch == Arch.AMD64,
    #   lambda: netboot("HTTPv6"),     "HTTPv6: MAAS->openSUSE chainload"),
  ]

  for predicate, do_task, msg in TASKS:
    if predicate():
      do_task()
      print(f"{msg} OK")
