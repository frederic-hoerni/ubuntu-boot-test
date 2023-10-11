#
# ubuntu-boot-test: autopkgtest_run.py: Autopkgtest runner
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
packages = [
  "shim-signed"
]

if ARCH == "amd64":
  packages.append("grub-efi-amd64-bin")
  packages.append("grub-efi-amd64-signed")
elif ARCH == "aarch64":
  packages.append("grub-efi-arm64-bin")
  packages.append("grub-efi-arm64-signed")
else:
  assert False, f"Invalid architecture {ARCH}"

# Download packages
package_dir = tempfile.TemporaryDirectory("")
package_path = package_dir.name

subprocess.run(
  ["apt-get", "download"] + packages,
  cwd=package_path)

if DEBUG:
  print(f"Downloaded packages to            {package_path}")

# Create virtual machine
vm = UEFIVm(RELEASE, ARCH, package_path)

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
  proc.sendline("apt install /mnt/*.deb && echo ok")
  proc.expect("ok\r\r\n")

  # Reboot
  proc.expect("# ")
  proc.sendline("reboot")
  # Check for successful reboot, then power off
  do_login(proc)
  proc.expect("# ")
  proc.sendline("poweroff")


vm.run(test2)
print("Boot with upgraded packages       OK")

# After all is done, give us a shell in debug mode
if DEBUG:
  vm.run(lambda proc: proc.interact())
