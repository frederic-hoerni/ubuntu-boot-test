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
import typing as tp
from time import sleep

from ubuntu_boot_test import config, util
from ubuntu_boot_test.tpm import TPM
from ubuntu_boot_test.vm import VirtualMachine


class EncryptCloudImage:
    """Singleton class to download and execute encrypt-cloud-image."""

    # NOTE: Someone can easily `EncryptCloudImage._instance = None` and
    # this is no longer a "Singleton", but the idea is that there should
    # not be multiple downloads of encrypt-cloud-image regardless of how
    # many CVMs are downloaded or encrypted.

    _instance = None

    def __new__(cls, install_dir: str):
        if cls._instance is None:
            # Only allow one instance
            cls._instance = super(EncryptCloudImage, cls).__new__(cls)
        return cls._instance

    def __init__(self, install_dir: str):
        """
        Downloads the encrypt_cloud_image package in order to encrypt the
        CVM image.
        """
        EncryptCloudImage._ensure_nbd_loaded()
        # Download encrypt-cloud-image and mark it executable
        eci_path = os.path.join(install_dir, "encrypt-cloud-image")
        util.github_download_release_asset(
            "canonical/encrypt-cloud-image", "latest", "encrypt-cloud-image", eci_path
        )
        util.runcmd(["chmod", "+x", eci_path])
        self.executable = eci_path

    def encrypt(
        self,
        image_dir: str,
        image_name: str,
        encrypted_image_name: str,
        cwd: str,
    ) -> None:
        # Encrypt image
        util.runcmd(
            [
                self.executable,
                "encrypt",
                "--override-datasources",
                "NoCloud",
                "-o",
                os.path.join(image_dir, encrypted_image_name),
                os.path.join(image_dir, image_name),
            ],
            cwd=cwd,
        )

    def deploy(
        self,
        uefi_config_path: str,
        encrypted_image_name: str,
        srk_path: str,
        cwd: str,
    ) -> None:
        util.runcmd(
            [
                self.executable,
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
            cwd=cwd,
        )

    @staticmethod
    def _ensure_nbd_loaded() -> None:
        """
        Checks that the nbd module is loaded; required for CVM encryption.
        """
        lsmod_result = subprocess.run(["lsmod"], stdout=subprocess.PIPE)
        if b"nbd" not in lsmod_result.stdout:
            if os.getuid() != 0:
                assert False, "Need root to load nbd"
            if subprocess.run(["modprobe", "nbd"]).returncode != 0:
                assert False, "Failed to load nbd"


class ConfidentialVirtualMachine(VirtualMachine):

    def __init__(
        self,
        cwd: str,
        image_url: str,
        arch: util.Arch,
        firmware: util.Firmware,
    ):
        super().__init__(cwd, image_url, arch, firmware)
        self.tpm = TPM(cwd)
        # Generates "srk.pub" in cwd
        self.tpm.generate_srk(cwd)

    def start(self) -> None:  # type: ignore[override]
        """Start the CVM with the TPM activated. Timeout after 30 minutes."""
        super().start(tpm=self.tpm)

    def reboot(self, wait_seconds: int = 0) -> None:
        """Reboot the CVM and TPM processes."""

        # NOTE: Using the reboot command limits stdout, so
        # we shutdown and start instead
        self.shutdown()

        # shutdown turns off the TPM for some undocumented reason, so it
        # needs to be restarted as well
        self.tpm.run_process()
        self.start()
        if wait_seconds:
            print(f"Giving CVM {wait_seconds}s to initialize...")
            sleep(wait_seconds)
            print("Returning control")

    @staticmethod
    def prepare_cvm_image(install_dir: str, release: str, arch: util.Arch) -> str:
        """
        Remotely pulls the CVM from the cloud storage location, unpacks it,
        and truncates it for use in QEMU.
        """
        vhd_tarball = os.path.join(install_dir, "vhd.tar.gz")

        # Download and CVM vhd tarball
        util.download_file(util.cvm_cloud_url(release, arch), vhd_tarball)
        util.runcmd(["tar", "xf", vhd_tarball], cwd=install_dir)

        vhd_name = "livecd.ubuntu-cpc.azure.fde.vhd"
        # Resize the VHD while it is sparse so that it actually fits on disk once encrypted
        util.runcmd(
            ["truncate", "-s", "4G", vhd_name],
            cwd=install_dir,
        )
        return vhd_name


def write_uefi_config(vm: VirtualMachine) -> str:
    """
    Writes the static UEFI configuration data for use in the virtual
    machine. This must be run before the initial boot/provision or
    there is no effect.
    """
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
    uefi_config_path = os.path.join(vm._vmdir, "uefi.json")
    with open(uefi_config_path, "w") as f:
        if config.DEBUG:
            print("Using UEFI config", json.dumps(uefi_config))
        f.write(json.dumps(uefi_config))

    return uefi_config_path


def install_new_packages(vm: VirtualMachine, package_paths: tp.List[str]) -> None:
    """Copies provided packages to the CVM and installs them."""
    # Copy packages to VM
    vm.copy_files(package_paths, "/tmp/")
    # Install new packages
    vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])


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


def execute(args):
    tempdir = tempfile.TemporaryDirectory("")

    PACKAGE_SETS = {
        util.Arch.AMD64: set(("nullboot",)),
    }

    # Prepare packages for install
    package_paths = util.prepare_packages(
        tempdir.name, PACKAGE_SETS[args.arch], args.packages
    )

    # Download ECI before image to ensure NBD is loaded before setting up
    # VM infrastructure (which can be time-consuming)
    eci = EncryptCloudImage(tempdir.name)
    print("Initialized encrypt-cloud-image")

    cvm_image_name = ConfidentialVirtualMachine.prepare_cvm_image(
        tempdir.name, args.release, args.arch
    )
    print("Downloaded and truncated CVM Image")

    # Create virtual machine
    cvm = ConfidentialVirtualMachine(tempdir.name, None, args.arch, util.Firmware.UEFI)
    print("Initialized VM Environment")

    uefi_config_path = write_uefi_config(cvm)
    print("Wrote UEFI Config")

    # VirtualMachine uses "disk.img" as the name of the image
    encrypted_image_name = "disk.img"
    eci.encrypt(
        image_dir=tempdir.name,
        image_name=cvm_image_name,
        encrypted_image_name=encrypted_image_name,
        cwd=tempdir.name,
    )
    print("Encrypted CVM Image")
    eci.deploy(
        uefi_config_path=uefi_config_path,
        encrypted_image_name=encrypted_image_name,
        srk_path="srk.pub",
        cwd=tempdir.name,
    )
    print("Deployed CVM Image")

    installnew = lambda: install_new_packages(cvm, package_paths)

    TASKS = [
        (lambda: True, cvm.start, "Boot and provision image"),
        (lambda: True, installnew, "Install new nullboot"),
        (lambda: True, cvm.reboot, "Shut down and reboot virtual machine"),
        (lambda: True, cvm.shutdown, "Shut down"),
    ]

    for predicate, do_task, msg in TASKS:
        if predicate():
            do_task()
            print(f"{msg} OK")
