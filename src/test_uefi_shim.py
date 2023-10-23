#
# ubuntu-boot-test: test_uefi_shim.py: UEFI shim boot test
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
import tempfile

TEMPDIR = tempfile.TemporaryDirectory("")

PACKAGE_SETS = {
  Arch.AMD64: set(("shim-signed",
                   "grub-efi-amd64-bin",
                   "grub-efi-amd64-signed")),
  Arch.ARM64: set(("shim-signed",
                   "grub-efi-arm64-bin",
                   "grub-efi-arm64-signed")),
}

# Parse arguments
parser = argparse.ArgumentParser(prog="test_uefi_shim.py",
  description="UEFI shim boot test")

parser.add_argument("-r", "--release", required=True,
  help="Guest Ubuntu release")
parser.add_argument("-a", "--arch", required=True, type=Arch,
  help="Guest architecture")
parser.add_argument("-s", "--secboot", required=True, type=Secboot,
  help="Secure Boot configuration")
parser.add_argument("packages", nargs="*",
  help="List of packages to install (instead of apt-get download)")

args = parser.parse_args()

# Package paths to install
package_paths = prepare_packages(TEMPDIR.name, PACKAGE_SETS[args.arch], args.packages)

# Create virtual machine
vm = VirtualMachine(args.release, args.arch, Firmware.UEFI, args.secboot != Secboot.NONE)

def setupsbkeys():
  # db
  pem_priv, pem_cert, esl_cert = gen_efi_signkey()
  with open(os.path.join(TEMPDIR.name, "db.key"), "wb") as f:
    f.write(pem_priv)
  with open(os.path.join(TEMPDIR.name, "db.pem"), "wb") as f:
    f.write(pem_cert)
  vm.write_efivar(EFI_IMAGE_SECURITY_DATABASE_GUID, "db", esl_cert, append=False)

  # MokList
  pem_priv, pem_cert, esl_cert = gen_efi_signkey()
  with open(os.path.join(TEMPDIR.name, "MokList.key"), "wb") as f:
    f.write(pem_priv)
  with open(os.path.join(TEMPDIR.name, "MokList.pem"), "wb") as f:
    f.write(pem_cert)
  vm.write_efivar(SHIM_LOCK_GUID, "MokList", esl_cert, append=False)

def disablesb():
  vm.write_efivar(SECURE_BOOT_ENABLE_GUID, "SecureBootEnable", b"\x00", append=False)

def enablesb():
  vm.write_efivar(SECURE_BOOT_ENABLE_GUID, "SecureBootEnable", b"\x01", append=False)

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

def installnew():
  # Copy packages to VM
  vm.copy_files(package_paths, "/tmp/")
  # Install packages
  vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])
  # Install new GRUB
  vm.run_cmd(["grub-install"])

def cleansigs(path):
  # Remove exisiting signatures from the binary
  while subprocess.run(["sbattach", "--remove", path]).returncode == 0:
    pass

def sbsign_file(with_key, path):
  cleansigs(path)
  result = subprocess.run(["sbsign",
    "--key", os.path.join(TEMPDIR.name, f"{with_key}.key"),
    "--cert", os.path.join(TEMPDIR.name, f"{with_key}.pem"),
    "--output", path,
    path])
  assert result.returncode == 0, f"Failed to sign {path}"

if args.arch == Arch.AMD64:
  SHIM_PATH = "/boot/efi/EFI/ubuntu/shimx64.efi"
  GRUB_PATH = "/boot/efi/EFI/ubuntu/grubx64.efi"
else:
  SHIM_PATH = "/boot/efi/EFI/ubuntu/shima64.efi"
  GRUB_PATH = "/boot/efi/EFI/ubuntu/gruba64.efi"

def sbsignnew():
  PATHS = [ ("db", SHIM_PATH), ("MokList", GRUB_PATH) ]

  # Get files from VM
  vm.get_files(list(map(lambda x: x[1], PATHS)), TEMPDIR.name)

  for with_key, remote_path in PATHS:
    local_path = os.path.join(TEMPDIR.name, os.path.split(remote_path)[-1])
    # Sign
    sbsign_file(with_key, local_path)
    # Write back
    vm.copy_files([ local_path ], remote_path)

def ephemeralboot():
  vm.start(ephemeral_snapshot=True)
  vm.waitboot()

def sbstripshim():
  # Boot as an ephermeral snapshot
  ephemeralboot()
  # Clean signatures from shim
  vm.get_files([SHIM_PATH], TEMPDIR.name)
  local_shim_path = os.path.join(TEMPDIR.name, os.path.split(SHIM_PATH)[-1])
  cleansigs(local_shim_path)
  vm.copy_files([local_shim_path], SHIM_PATH)
  # Reboot and verify that we get "Access denied"
  rebootvm()

  # Shut down
  shutdownvm()

TASKS = [
  (lambda: args.secboot == Secboot.SIGN,
    setupsbkeys,  "Generate and enrol Secure Boot keys"),
  (lambda: args.secboot == Secboot.SIGN,
    disablesb,    "Disable Secure Boot"),
  (lambda: True,
    bootvm,       "Boot and provision image"),
  (lambda: True,
    installnew,   "Install new bootloaders"),
  (lambda: args.secboot == Secboot.SIGN,
    sbsignnew,    "Sign new bootloaders"),
  (lambda: True,
    shutdownvm,   "Shut down virtual machine"),
  (lambda: args.secboot == Secboot.SIGN,
    enablesb,     "Enable Secure Boot"),
  (lambda: True,
    bootvm,       "Boot with new bootloaders"),
  (lambda: True,
    shutdownvm,   "Shut down virtual machine"),
  #(lambda: args.secboot != Secboot.NONE,
  #  sbstripshim,  "Secure Boot: stripping shim signature"),
]

for predicate, do_task, msg in TASKS:
  if predicate():
    do_task()
    print(f"{msg} OK")
