# ubuntu-boot-test: cmd_uefi_shim.py: UEFI shim boot test
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

import base64
import json
import os
import subprocess
import tempfile

from ubuntu_boot_test import config, util
from ubuntu_boot_test.tpm import TPM
from ubuntu_boot_test.vm import VirtualMachine


def register(subparsers):
    parser = subparsers.add_parser("nullboot", description="nullboot boot test")

    parser.add_argument("-r", "--release", required=True, help="Guest Ubuntu release")
    parser.add_argument(
        "-a", "--arch", required=True, type=util.Arch, help="Guest architecture"
    )
    parser.add_argument(
        "packages",
        nargs="*",
        help="List of packages to install (instead of apt-get download)",
    )


def ensure_nbd_loaded():
    lsmod_result = subprocess.run(["lsmod"], stdout=subprocess.PIPE)
    if b"nbd" not in lsmod_result.stdout:
        if os.getuid() != 0:
            assert False, "Need root to load nbd"
        if subprocess.run(["modprobe", "nbd"]).returncode != 0:
            assert False, "Failed to load nbd"


def execute(args):
    tempdir = tempfile.TemporaryDirectory("")

    PACKAGE_SETS = {
        util.Arch.AMD64: set(("nullboot",)),
    }

    # Make sure the NBD module is present
    ensure_nbd_loaded()

    # Prepare packages for install
    package_paths = util.prepare_packages(
        tempdir.name, PACKAGE_SETS[args.arch], args.packages
    )

    # Download and CVM vhd tarball
    vhd_tarball = os.path.join(tempdir.name, "vhd.tar.gz")
    util.download_file(util.cvm_cloud_url(args.release, args.arch), vhd_tarball)
    util.runcmd(["tar", "xf", vhd_tarball], cwd=tempdir.name)

    # Resize the VHD while it is sparse so that it actually fits on disk once encrypted
    util.runcmd(
        ["truncate", "-s", "4G", "livecd.ubuntu-cpc.azure.fde.vhd"], cwd=tempdir.name
    )

    # Download encrypt-cloud-image and mark it executable
    eci_path = os.path.join(tempdir.name, "encrypt-cloud-image")
    util.github_download_release_asset(
        "canonical/encrypt-cloud-image", "latest", "encrypt-cloud-image", eci_path
    )
    util.runcmd(["chmod", "+x", eci_path])

    # Create virtual machine
    vm = VirtualMachine(tempdir.name, None, args.arch, util.Firmware.UEFI)
    # Create UEFI config for VM
    uefi_config = {
        "PK": base64.b64encode(
            vm.read_efivar(util.EFI_GLOBAL_VARIABLE_GUID, "PK")
        ).decode(),
        "KEK": base64.b64encode(
            vm.read_efivar(util.EFI_GLOBAL_VARIABLE_GUID, "KEK")
        ).decode(),
        "db": base64.b64encode(
            vm.read_efivar(util.EFI_IMAGE_SECURITY_DATABASE_GUID, "db")
        ).decode(),
        "dbx": base64.b64encode(
            vm.read_efivar(util.EFI_IMAGE_SECURITY_DATABASE_GUID, "dbx")
        ).decode(),
        "omitsReadyToBootEvent": False,
    }
    uefi_config_path = os.path.join(tempdir.name, "uefi.json")
    with open(uefi_config_path, "w") as f:
        if util.DEBUG:
            print("Using UEFI config", json.dumps(uefi_config))
        f.write(json.dumps(uefi_config))

    # Create virtual TPM
    tpm = TPM(tempdir.name)
    # Generate SRK
    tpm.generate_srk(tempdir.name)
    # Encrypt image
    util.runcmd(
        [
            eci_path,
            "encrypt",
            "--override-datasources",
            "NoCloud",
            "-o",
            "disk.img",
            "livecd.ubuntu-cpc.azure.fde.vhd",
        ],
        cwd=tempdir.name,
    )
    util.runcmd(
        [
            eci_path,
            "deploy",
            "--srk-pub",
            "srk.pub",
            "--uefi-config",
            uefi_config_path,
            "--add-efi-boot-manager-profile",
            "--add-efi-secure-boot-profile",
            "--add-ubuntu-kernel-profile",
            "disk.img",
        ],
        cwd=tempdir.name,
    )

    def installnew():
        # Copy packages to VM
        vm.copy_files(package_paths, "/tmp/")
        # Install new packages
        vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])

    TASKS = [
        (lambda: True, lambda: vm.start(tpm=tpm), "Boot and provision image"),
        (lambda: True, installnew, "Install new nullboot"),
        (lambda: True, vm.shutdown, "Shut down virtual machine"),
        # NOTE: this is a bit hacky but we need to restart the vTPM
        # process here because swtpm exits when qemu shuts down for
        # some undocumented reason
        (lambda: True, tpm.run_process, "Restart vTPM process"),
        (lambda: True, lambda: vm.start(tpm=tpm), "Boot with new nullboot"),
        (lambda: True, vm.shutdown, "Shut down virtual machine"),
    ]

    for predicate, do_task, msg in TASKS:
        if predicate():
            do_task()
            print(f"{msg} OK")
