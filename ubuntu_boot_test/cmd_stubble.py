# ubuntu-boot-test: cmd_stubble.py: stubble test
#
# Copyright (C) 2025 Canonical, Ltd.
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
  parser = subparsers.add_parser("stubble", description="stubble test")

  parser.add_argument("-r", "--release", required=True,
    help="Guest Ubuntu release")
  parser.add_argument("-a", "--arch", required=True, type=Arch,
    help="Guest architecture")
  parser.add_argument("packages", nargs="*",
    help="List of packages to install (instead of apt-get download)")

def execute(args):
  TEMPDIR = tempfile.TemporaryDirectory("")

  PACKAGE_SETS = {
    Arch.AMD64: set(("stubble", "systemd-ukify", "python3-pefile", "python3-zstandard", )),
    Arch.ARM64: set(("stubble", "systemd-ukify", "python3-pefile", "python3-zstandard", )),
  }

  # Package paths to install
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

  gen_sb_key(SHIM_LOCK_GUID, "MokList")

  def sbsign_file(with_key, path):
    result = subprocess.run(["sbsign",
      "--key", os.path.join(TEMPDIR.name, f"{with_key}.key"),
      "--cert", os.path.join(TEMPDIR.name, f"{with_key}.pem"),
      "--output", path, path], capture_output=not DEBUG)
    assert result.returncode == 0, f"Failed to sign {path}"

  def installnew():
    # Copy packages to VM
    vm.copy_files(package_paths, "/tmp/")
    # Install packages
    vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])

  def wrapkernel():
    # Wrap kernel in stubble
    vm.run_cmd(["bash", "-c", "\"zcat -f /boot/vmlinuz > /boot/vmlinuz.tmp\""])
    vm.run_cmd([
      "ukify", "build", "--linux=/boot/vmlinuz.tmp", "--stub=/usr/lib/stubble/stubble.efi", "--output=/boot/vmlinuz.efi"
    ])
    # Replace original kernel
    real_kernel_path = vm.run_cmd(["readlink", "-f", "/boot/vmlinuz"]).strip()
    vm.run_cmd(["mv", "/boot/vmlinuz.efi", real_kernel_path])

  def checkunsigned():
    # Reboot and wait for error from GRUB
    # To make sure it does not boot before signing
    vm.reboot(wait=False)
    vm.waitserial(b"error: bad shim lock signature.") # FIXME: this will change for loader protocol

    # Force shutdown VM
    vm.forceshutdown()

  def signstubble():
    real_kernel_path = vm.run_cmd(["readlink", "-f", "/boot/vmlinuz"]).strip()
    with vm.remote_file(real_kernel_path) as rf:
      sbsign_file("MokList", rf.local_path)

  TASKS = [
    (vm.start,      "Boot and provision image"),
    (installnew,    "Install new packages"),
    (wrapkernel,    "Wrap kernel in stubble"),
    (checkunsigned, "Ensure unsinged stubble does not boot"),
    (vm.disablesb,  "Disable SecureBoot"),
    (vm.start,      "Boot virtual machine"),
    (signstubble,   "Sign stubble image"),
    (vm.shutdown,   "Shut down virtual machine"),
    (vm.enablesb,   "Enable SecureBoot"),
    (vm.start,      "Boot with signed stubble image"),
    (vm.shutdown,   "Shut down virtual machine"),
  ]

  for do_task, msg in TASKS:
    do_task()
    print(f"{msg} OK")
