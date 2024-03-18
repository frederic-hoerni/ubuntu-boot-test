# ubuntu-boot-test: cmd_pcbios_mass.py: MAAS style netboot test (PC-BIOS)
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
  parser = subparsers.add_parser("pcbios_maas",
    description="MAAS style netboot test (PC-BIOS)")

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

  PACKAGE_SET = set((
    "grub2-common",
    "grub-common",
    "grub-pc",
    "grub-pc-bin"
  ))

  # Download MAAS stable loader
  def fetch_maas_loaders():
    stream = json.loads(fetch_file(MAAS_STABLE_STREAM_URL))
    products = stream["products"]["com.ubuntu.maas.stable:1:pxelinux:pxe:amd64"]
    versions = max(products["versions"].items(), key=lambda x: x[0])[1]
    download_file(f"{MAAS_IMAGES_URL}/" + versions["items"]["syslinux"]["path"],
                  os.path.join(TEMPDIR.name, "syslinux.tar.xz"))
    os.mkdir(os.path.join(TEMPDIR.name, "syslinux"))
    runcmd(["tar", "-xf", os.path.join(TEMPDIR.name, "syslinux.tar.xz")],
      cwd=os.path.join(TEMPDIR.name, "syslinux"))

  fetch_maas_loaders()

  # Netboot directory
  netbootdir = os.path.join(TEMPDIR.name, "netboot")
  os.mkdir(netbootdir)

  # Deploy PXELINUX components to netboot server
  for file in ("pxelinux.0", "ldlinux.c32"):
    shutil.copy(
      os.path.join(TEMPDIR.name, "syslinux", file),
      os.path.join(netbootdir, file))

  # Write "pxelinux.cfg"
  NETBOOT_PXELINUXCFG = """\
DEFAULT local

LABEL local
  LOCALBOOT 0
"""
  os.mkdir(os.path.join(netbootdir, "pxelinux.cfg"))
  with open(os.path.join(netbootdir, "pxelinux.cfg", "default"), "w") as f:
    f.write(NETBOOT_PXELINUXCFG)

  # Setup netboot environment
  vnet = VirtualNetwork(TEMPDIR.name, netbootdir)
  tap = vnet.new_tap()

  # Prepare packages for install
  package_paths = prepare_packages(TEMPDIR.name, PACKAGE_SET, args.packages)

  # Create virtual machine
  vm = VirtualMachine(TEMPDIR.name, args.release, args.arch, Firmware.PCBIOS)

  def netboot():
    vm.start(tapname=tap)
    vm.shutdown()

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

  TASKS = [
    # This is the only test we really care about here
    # Aka using the PXELINUX loader in the archive to chainload a local copy
    # of the new GRUB.
    # Testing new versions of PXELINUX is currently outside our scope.
    (installnew,                    "Deploy tested bootloaders to image"),
    (netboot,                       "PXEv4: MAAS->Tested chainload"),
  ]

  for do_task, msg in TASKS:
    do_task()
    print(f"{msg} OK")
