#
# ubuntu-boot-test: autopkgtest_efi.py: autopkgtest for EFI boot
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

import tempfile
import subprocess
import sys
from config import *
from uefi_vm import UEFIVm

# Figure out what base packages we need
packages = {
  Arch.AMD64: [
    "shim-signed",
    "grub-efi-amd64-bin",
    "grub-efi-amd64-signed"
  ],
  Arch.ARM64: [
    "shim-signed",
    "grub-efi-arm64-bin",
    "grub-efi-arm64-signed"
  ]
}

# Download packages
package_dir = tempfile.TemporaryDirectory("")
subprocess.run(["apt-get", "download"] + packages[ARCH], cwd=package_dir.name)
if DEBUG:
  print(f"Downloaded packages to            {package_dir.name}")

# Create virtual machine
vm = UEFIVm(RELEASE, ARCH, package_dir.name)

def do_login(proc):
  proc.expect("login: ")
  proc.sendline(USERNAME)
  proc.expect("Password: ")
  proc.sendline(PASSWORD)

# Test #1: make sure we can boot and log in
def test1(proc):
  do_login(proc)
  proc.expect("# ")
  proc.sendline("poweroff")

vm.run(test1)
print("Cloud-init + first boot           OK")

# Test #2: install new packages
def test2(proc):
  # Login and update packages
  do_login(proc)
  proc.expect("# ")
  proc.sendline(f"mount LABEL={CILABEL} /mnt")
  proc.expect("# ")
  proc.sendline("apt install /mnt/*.deb && echo APTRESULT$?")
  proc.expect("APTRESULT0\r\r\n")

  # Reboot
  proc.expect("# ")
  proc.sendline("reboot")
  # Check for successful reboot, then power off
  do_login(proc)
  proc.expect("# ")
  proc.sendline("poweroff")


vm.run(test2)
print("Boot with upgraded packages       OK")

# Boot and get an interactive shell when in debug mode
if DEBUG:
  def debug(proc):
    proc.logfile = None
    proc.interact()
  vm.run(debug)
