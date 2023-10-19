#
# ubuntu-boot-test: run.py: Test runner entry point
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
import subprocess
import sys
import tempfile

TEMPDIR = tempfile.TemporaryDirectory("")

# Parse arguments
parser = argparse.ArgumentParser(
  prog='ubuntu-boot-test',
  description='End-to-end Ubuntu bootloader test')

parser.add_argument("-r", "--release", required=True,
  help="Guest Ubuntu release")
parser.add_argument("-a", "--arch", required=True, type=Arch,
  help="Guest architecture")
parser.add_argument("-f", "--firmware", required=True, type=Firmware,
  help="Guest firmware type")
parser.add_argument("-s", "--secboot", type=Secboot, default=Secboot.NONE,
  help="Secure Boot configuration (only meaningful with -f uefi)")
parser.add_argument("--no-shim", action="store_true",
  help="Use GRUB without installing shim (only meaningful with -f uefi)")
parser.add_argument("packages", nargs="*",
  help="List of packages to install (instead of apt-get download)")

args = parser.parse_args()

# Validate arguments
if args.firmware != Firmware.UEFI and args.secboot != Secboot.NONE:
  print(f"Firmware {args.firmware} cannot use secboot {args.secboot}",
        file=sys.stderr)
  exit(1)

if (args.firmware != Firmware.UEFI or args.secboot != Secboot.NONE) and \
    args.no_shim:
  print(f"--no-shim cannot be used with firmware {args.firmware}" \
         f" and secboot mode {args.secboot}",
    file=sys.stderr)
  exit(1)

# Retrieve package set for (arch, firmware, no-shim)

PACKAGE_SETS = {
  (Arch.AMD64, Firmware.UEFI, True): set(("grub-efi-amd64-bin", )),
  (Arch.AMD64, Firmware.UEFI, False): set(("shim-signed",
                                          "grub-efi-amd64-bin",
                                          "grub-efi-amd64-signed")),
  (Arch.AMD64, Firmware.PCBIOS, False): set(("grub-pc", )),
  (Arch.ARM64, Firmware.UEFI, False): set(("shim-signed",
                                          "grub-efi-arm64-bin",
                                          "grub-efi-arm64-signed")),
  (Arch.ARM64, Firmware.UEFI, True): set(("grub-efi-arm64-bin", ))
}

if (args.arch, args.firmware, args.no_shim) not in PACKAGE_SETS:
  print(f"Invalid combination of --arch {args.args}, --firmware {args.firmware}"
    + "--no-shim" if args.no_shim else "", file=sys.stderr)
  exit(1)


package_set = PACKAGE_SETS[(args.arch, args.firmware, args.no_shim)]

if len(args.packages) > 0:
  # Validate packages
  actual_package_set = set()
  for package_path in args.packages:
    _, filename = os.path.split(package_path)
    packagename = filename.split("_")[0]
    actual_package_set.add(packagename)
  if actual_package_set != package_set:
    print("Invalid package set provided " \
          f"(actual {actual_package_set}, expecting {package_set})",
          file=sys.stderr)
    exit(1)
  # Packages are already in the filesystem
  package_paths = args.packages
else:
  # Download packages
  download_packages(package_set, TEMPDIR.name)
  # List package file paths
  package_paths = [
    os.path.join(TEMPDIR.name, package_filename)
      for package_filename in os.listdir(TEMPDIR.name)
      if package_filename.endswith("deb")
  ]

# Create virtual machine
vm = VirtualMachine(args.release,
                    args.arch,
                    args.firmware,
                    args.secboot)

vm.write_efivar(EFI_IMAGE_SECURITY_DATABASE_GUID, "db", b"", append=False)

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
  (lambda: True,
    bootvm,      "Boot and provision image"),
  (lambda: args.no_shim,
    purgesigned, "Purge signed bootloaders"),
  (lambda: True,
    installnew,  "Install new bootloaders"),
  (lambda: True,
    rebootvm,    "Reboot with new bootloaders"),
  (lambda: True,
    shutdownvm,  "Shut down virtual machine"),
]

for predicate, do_task, msg in TASKS:
  if predicate():
    do_task()
    print(f"{msg} OK")
