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
import shutil
import sys
import tempfile

# Packages we need for each architecture+firmware combination
PER_ARCH_FW_PACKAGES = {
  (Arch.AMD64, Firmware.UEFI): [
    "shim-signed",
    "grub-efi-amd64-bin",
    "grub-efi-amd64-signed"
  ],
  (Arch.AMD64, Firmware.PCBIOS): [
    "grub-pc",
  ],
  (Arch.ARM64, Firmware.UEFI): [
    "shim-signed",
    "grub-efi-arm64-bin",
    "grub-efi-arm64-signed"
  ]
}

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

# Collect paackages
package_dir = tempfile.TemporaryDirectory("")

if DEBUG:
  print(f"Storing packages in {package_dir.name}")

if len(args.packages) > 0:
  # Copy packages
  for package_path in args.packages:
    if args.no_shim and "shim" in package_path.split("/")[-1]:
      print("Warning: Got shim package and --no-shim at once")
    shutil.copy(package_path, package_dir.name)
else:
  # Download packages
  download_packages(
    PER_ARCH_FW_PACKAGES[(args.arch, args.firmware)], package_dir.name)

# Create virtual machine
vm = VirtualMachine(args.release,
                    args.arch,
                    args.firmware,
                    args.secboot,
                    package_dir.name)

def do_login(proc):
  proc.expect("login: ")
  proc.sendline(USERNAME)
  proc.expect("Password: ")
  proc.sendline(PASSWORD)

def do_shell(proc, cmd):
  proc.expect("# ")
  proc.sendline(cmd)

# Test #1: make sure we can boot and log in
def test1(proc):
  do_login(proc)
  do_shell(proc, "poweroff")

# Test #2: install new packages
def test2(proc):
  # Login
  do_login(proc)

  if args.no_shim:
    # Purge signed packages if in unsigned mode
    # We do this to make doubly sure the new GRUB is the only
    # thing in the boot path.
    # Note that this shouldn't be confused with running snakeoil signed
    # shim and GRUB for testing.
    do_shell(proc, "apt purge --allow-remove-essential -y grub-*-signed shim-signed")
    do_shell(proc, "rm -r /boot/efi/*")

  # Install packages to be tested
  do_shell(proc, f"mount LABEL={CILABEL} /mnt")
  do_shell(proc, "apt install -y /mnt/*.deb && echo APTRESULT$?")
  proc.expect("APTRESULT0\r\r\n")
  do_shell(proc, "grub-install")

  # Reboot
  do_shell(proc, "reboot")

  # Login to check for successful reboot, then power off
  do_login(proc)
  do_shell(proc, "poweroff")


vm.run(test1)
print("Boot with original packages OK")
vm.run(test2)
print("Boot with upgraded packages OK")

# Boot and get an interactive shell when in debug mode
if DEBUG:
  def debug(proc):
    proc.logfile = None
    proc.interact()
  vm.run(debug)
