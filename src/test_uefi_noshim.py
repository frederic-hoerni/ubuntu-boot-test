#
# ubuntu-boot-test: test_uefi_noshim.py: UEFI no shim boot test
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

from config import *
from util import *
from vm import VirtualMachine
import argparse
import tempfile

TEMPDIR = tempfile.TemporaryDirectory("")

PACKAGE_SETS = {
  Arch.AMD64: set(("grub-efi-amd64-bin", )),
  Arch.ARM64: set(("grub-efi-arm64-bin", )),
}

# Parse arguments
parser = argparse.ArgumentParser(prog="test_uefi_noshim.py",
  description="UEFI no shim boot test")

parser.add_argument("-r", "--release", required=True,
  help="Guest Ubuntu release")
parser.add_argument("-a", "--arch", required=True, type=Arch,
  help="Guest architecture")
parser.add_argument("packages", nargs="*",
  help="List of packages to install (instead of apt-get download)")

args = parser.parse_args()

# Package paths to install
package_paths = prepare_packages(TEMPDIR.name, PACKAGE_SETS[args.arch], args.packages)

# Create virtual machine
vm = VirtualMachine(args.release, args.arch, Firmware.UEFI, secboot=False)

def bootvm():
  # Wait for VM to start and cloud-init to apply
  vm.start()
  vm.waitboot()

def rebootvm():
  # Wait for successful reboot
  vm.run_cmd(["reboot"], assert_ok=False)
  vm.waitboot()

def shutdownvm():
  # Wait for successful shutdown
  vm.run_cmd(["poweroff"], assert_ok=False)
  vm.waitshutdown()

def purgesigned():
  # Purge signed packages if in unsigned mode
  # We do this to make doubly sure the new GRUB is the only
  # thing in the boot path.
  # Note that this shouldn't be confused with running snakeoil signed
  # shim and GRUB for testing.
  vm.run_cmd(["apt", "purge", "--allow-remove-essential", "--yes",
                "grub-*-signed", "shim-signed"])
  # Also remove the EFI directoiry from the ESP to make sure no previous
  # shim remains
  vm.run_cmd(["rm", "-r", "/boot/efi/EFI"])

def installnew():
  # Copy packages to VM
  vm.copy_files(package_paths, "/tmp/")
  # Install packages
  vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])
  # Install new GRUB
  vm.run_cmd(["grub-install"])

TASKS = [
  (bootvm,       "Boot and provision image"),
  (purgesigned,  "Purge signed bootloaders"),
  (installnew,   "Install new bootloaders"),
  (rebootvm,     "Reboot with new bootloaders"),
  (shutdownvm,   "Shut down virtual machine"),
]

for do_task, msg in TASKS:
  do_task()
  print(f"{msg} OK")
