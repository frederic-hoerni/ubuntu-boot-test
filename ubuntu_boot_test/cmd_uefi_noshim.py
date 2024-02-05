# ubuntu-boot-test: cmd_uefi_noshim.py: UEFI no shim boot test
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
import tempfile

def register(subparsers):
  # Parse arguments
  parser = subparsers.add_parser("uefi_noshim",
    description="UEFI no shim boot test")

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
      "grub-efi-amd64-bin"
    )),
    Arch.ARM64: set((
      "grub2-common",
      "grub-common",
      "grub-efi-arm64",
      "grub-efi-arm64-bin"
    )),
  }

  PURGE_SETS = {
    Arch.AMD64: set((
      "grub-efi-amd64-signed",
      "shim-signed"
    )),
    Arch.ARM64: set((
      "grub-efi-arm64-signed",
      "shim-signed"
    )),
  }

  # Package paths to install
  package_paths = prepare_packages(TEMPDIR.name, PACKAGE_SETS[args.arch], args.packages)
  # Create virtual machine
  vm = VirtualMachine(TEMPDIR.name, args.release, args.arch, Firmware.UEFI)
  vm.disablesb()

  def purgesigned():
    # Purge signed packages if in unsigned mode
    vm.run_cmd(["apt", "purge", "--allow-remove-essential", "--yes"] + list(PURGE_SETS[args.arch]))
    # Remove EFI directory to make sure no shim remains
    vm.run_cmd(["rm", "-r", "/boot/efi/EFI"])

  def installnew():
    # Copy packages to VM
    vm.copy_files(package_paths, "/tmp/")
    # Install packages
    vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])
    # Install new GRUB
    vm.run_cmd(["grub-install", "/dev/disk/by-id/virtio-0"])
    vm.run_cmd(["update-grub"])

  TASKS = [
    (vm.start,    "Boot and provision image"),
    (purgesigned, "Purge signed bootloaders"),
    (installnew,  "Install new bootloaders"),
    (vm.reboot,   "Reboot with new bootloaders"),
    (vm.shutdown, "Shut down virtual machine"),
  ]

  for do_task, msg in TASKS:
    do_task()
    print(f"{msg} OK")
