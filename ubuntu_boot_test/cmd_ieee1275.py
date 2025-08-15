# ubuntu-boot-test: cmd_ieee1275.py: POWER OpenFirmware boot test
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
from ubuntu_boot_test.util import *
from ubuntu_boot_test.vm import VirtualMachine
import tempfile

def register(subparsers):
  subparser = subparsers.add_parser("ieee1275",
    description="POWER OpenFirmware boot test")
  subparser.add_argument("-r", "--release", required=True,
    help="Guest Ubuntu release")
  subparser.add_argument("-a", "--arch", required=True, type=Arch,
    help="Guest architecture")
  subparser.add_argument("packages", nargs="*",
    help="List of packages to install (instead of apt-get download)")

def execute(args):
  PACKAGE_SET = set((
    "grub2-common",
    "grub-ieee1275",
    "grub-ieee1275-bin"
  ))

  TEMPDIR = tempfile.TemporaryDirectory("")

  # Package paths to install
  package_paths = prepare_packages(TEMPDIR.name, PACKAGE_SET, args.packages)
  # Create virtual machine
  vm = VirtualMachine(TEMPDIR.name, ubuntu_cloud_url(args.release, args.arch), args.arch, Firmware.IEEE1275)

  def installnew():
    # Copy packages to VM
    vm.copy_files(package_paths, "/tmp/")
    # Install packages
    vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])
    # Install new GRUB
    prep_dev = vm.run_cmd(["/usr/lib/grub/powerpc-ieee1275/prep-bootdev"])
    vm.run_cmd(["grub-install", prep_dev])
    vm.run_cmd(["update-grub"])

  TASKS = [
    (vm.start,    "Boot and provision image"),
    (installnew,  "Install new bootloaders"),
    (vm.reboot,   "Reboot with new bootloaders"),
    (vm.shutdown, "Shut down virtual machine"),
  ]

  for do_task, msg in TASKS:
    do_task()
    print(f"{msg} OK")
