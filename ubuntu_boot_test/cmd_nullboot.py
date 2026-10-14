# ubuntu-boot-test: cmd_nullboot.py: nullboot boot test
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
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import typing as tp
from dataclasses import dataclass
from pprint import pformat
from time import sleep

from ubuntu_boot_test import config, util
from ubuntu_boot_test.efibootvars import EFIVars
from ubuntu_boot_test.tpm import TPM
from ubuntu_boot_test.vm import VirtualMachine

# Required packages per architecture
PACKAGE_SETS = {
    util.Arch.AMD64: {"nullboot"},
}

# NOTE: We need to wait for the nullboot systemd service to finish running
# before we can query EFIVars on startup; otherwise, we cannot accurately
# test the state of different variables
NULLBOOT_WAIT_TIME = datetime.timedelta(seconds=15)

if config.DEBUG:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)


def boot_entry_hex_str(i: int) -> str:
    """Convert integer into BootEntry hex string format."""
    return f"{i:04X}"


def boot_order_hex_str(boot_order: tp.List[int]) -> str:
    """Convert integers in BootOrder into BootEntry hex string format."""
    return ",".join([boot_entry_hex_str(boot_num) for boot_num in boot_order])


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


@dataclass
class LocalCryptographicVariable:
    variable: bytes
    file_path: str

    def __post_init__(self):
        """
        Write the variable content to the file path provided during
        initialization.
        """
        if os.path.isfile(self.file_path):
            raise ValueError(
                f"Cannot write local cryptographic file: {repr(self.file_path)} already exists"
            )

        with open(self.file_path, "wb") as f:
            f.write(self.variable)


@dataclass
class EFISignature:
    """
    Maintains an EFI Signature, comprised of a private signing key and its
    public certificate as well as an EFI Signature List (ESL) formatted
    version of the certificate.
    """

    # Key Data
    local_pem_key: LocalCryptographicVariable
    local_pem_cert: LocalCryptographicVariable
    esl_cert: bytes

    @classmethod
    def x509_format(cls, key_dir: str):
        """
        Generates the private key, public certificate, and ESL-formatted
        certificate in the x509 format for digital signatures.
        """
        # esl -> EFI Signature List
        pem_key, pem_cert, esl_cert = util.gen_efi_signkey()

        pem_key_path = os.path.join(key_dir, "efi_signature.pem")
        pem_cert_path = os.path.join(key_dir, "efi_signature.cert")

        local_pem_key = LocalCryptographicVariable(pem_key, pem_key_path)
        local_pem_cert = LocalCryptographicVariable(pem_cert, pem_cert_path)

        return cls(
            local_pem_key=local_pem_key,
            local_pem_cert=local_pem_cert,
            esl_cert=esl_cert,
        )

    def register_to_vm(self, vm: VirtualMachine) -> None:
        """
        Registers the ESL-formatted certificate for use on the virtual
        machine.
        """
        vm.write_efivar(
            util.EFI_IMAGE_SECURITY_DATABASE_GUID, "db", self.esl_cert, append=True
        )


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
            LOGGER.debug(f"Giving CVM {wait_seconds}s to initialize...")
            sleep(wait_seconds)
            LOGGER.debug("Returning control")

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


def build_signed_bad_uki(efi_signature: EFISignature, version: int, cwd: str) -> str:
    """
    Constructs a UKI on the host machine that intentionally panics on boot,
    signs it for Secure Boot, and reports the path to the new kernel.
    """
    # NOTE: Using kernel from host OS simply because it is more convenient
    # The intent is for the kernel to panic, so it doesn't need to work on
    # the CVM, and it is easier than setting up ukify on the CVM
    release = os.uname().release
    linux_name = f"vmlinuz-{release}"
    efi_name = f"kernel.efi-{version}-broken-azure"
    cmd = [
        "sudo",
        "ukify",
        "build",
        "--cmdline",
        "console=ttyS0 panic=-1",
        "--linux",
        f"/boot/{linux_name}",
        "--output",
        efi_name,
        "--secureboot-private-key",
        efi_signature.local_pem_key.file_path,
        "--secureboot-certificate",
        efi_signature.local_pem_cert.file_path,
    ]
    response = subprocess.run(cmd, cwd=cwd)
    if response.returncode != 0:
        raise ValueError(f'Unable to create broken UKI via: {repr(" ".join(cmd))}')

    return os.path.join(cwd, efi_name)


def copy_host_kernel(vm: VirtualMachine, new_version: int) -> str:
    """
    Copies the existing kernel as the latest kernel (by version) to the
    specified virtual machine.
    """
    release = vm.run_cmd(
        [
            "uname",
            "-r",
        ]
    ).strip()  # remove newline
    kernel_path = f"/usr/lib/linux/efi/kernel.efi-{release}"
    new_kernel_path = f"/usr/lib/linux/efi/kernel.efi-{new_version}-good-azure"
    LOGGER.debug(f"Copying {repr(kernel_path)} to {repr(new_kernel_path)}")
    vm.run_cmd(
        [
            "cp",
            kernel_path,
            new_kernel_path,
        ]
    )
    return new_kernel_path


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
        LOGGER.debug("Using UEFI config %s" % json.dumps(uefi_config))
        f.write(json.dumps(uefi_config))

    return uefi_config_path


def install_new_packages(vm: VirtualMachine, package_paths: tp.List[str]) -> None:
    """Copies provided packages to the CVM and installs them."""
    # Copy packages to VM
    vm.copy_files(package_paths, "/tmp/")
    # Install new packages
    vm.run_cmd(["apt", "install", "--yes", "/tmp/*.deb"])


def register(subparsers) -> None:
    """Register this file for testing via CLI."""
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


def test_recovery_key(
    cvm: ConfidentialVirtualMachine,
) -> None:
    """
    This tests creating, listing and deleting recovery keys.
    """
    LOGGER.info("Test nullbootctl recovery-key starting")
    recovery_key1 = cvm.run_cmd(["nullbootctl", "recovery-key", "--create", "--name", "recovery-1"])
    assert re.match("[0-9]+-[0-9]+-[0-9]+-[0-9]+-[0-9]+-[0-9]+-[0-9]+-[0-9]+\n$", recovery_key1)
    recovery_key2 = cvm.run_cmd(["nullbootctl", "recovery-key", "--create", "--name", "recovery-2"])
    assert re.match("[0-9]+-[0-9]+-[0-9]+-[0-9]+-[0-9]+-[0-9]+-[0-9]+-[0-9]+\n$", recovery_key2)
    cvm.run_cmd(["nullbootctl", "recovery-key", "--list"])
    cvm.run_cmd(["nullbootctl", "recovery-key", "--verify"], input=recovery_key1.encode())
    cvm.run_cmd(["nullbootctl", "recovery-key", "--verify"], input=recovery_key2.encode())
    cvm.run_cmd(["nullbootctl", "recovery-key", "--delete", "--name", "recovery-1"])
    cvm.run_cmd(["nullbootctl", "recovery-key", "--verify"], input=recovery_key1.encode(), assert_ok=False)
    cvm.run_cmd(["nullbootctl", "recovery-key", "--verify"], input=recovery_key2.encode())
    cvm.run_cmd(["nullbootctl", "recovery-key", "--list"])
    cvm.run_cmd(["nullbootctl", "recovery-key", "--delete", "--name", "recovery-2"], assert_ok=False)
    cvm.run_cmd(["nullbootctl", "recovery-key", "--verify"], input=recovery_key2.encode())
    LOGGER.info("Run nullbootctl recovery-key OK")


def test_fallback_mechanism(
    cvm: ConfidentialVirtualMachine,
    efi_signature: EFISignature,
    latest_kernel_version: int,
    tempdir: tempfile.TemporaryDirectory,
) -> None:
    """
    This tests that the fallback mechanism is working as intended;
    i.e. the installation of a broken kernel does not update the BootOrder
    and only boots as BootNext to ensure a panic does not result in an
    infinite boot-loop on the first entry in BootOrder. Raises
    AssertionError if the logical portion of the test fails.
    """
    failures = []

    initial_bootvars = EFIVars.from_efibootmgr(cvm)

    LOGGER.debug("Generating signed, broken UKI")
    broken_uki_path = build_signed_bad_uki(
        efi_signature, latest_kernel_version, tempdir.name
    )
    LOGGER.debug("Generated signed, broken UKI")

    LOGGER.debug("Copying broken UKI to CVM")
    cvm.copy_files([broken_uki_path], "/usr/lib/linux/efi/")
    LOGGER.debug(f"Copied {repr(broken_uki_path)} to '/usr/lib/linux/efi/'")

    cvm.run_cmd(["nullbootctl"])
    LOGGER.info("Run nullbootctl OK")

    bad_kernel_bootvars = EFIVars.from_efibootmgr(cvm)
    if bad_kernel_bootvars.boot_order != initial_bootvars.boot_order:
        failures.append(
            {
                "msg": "BootOrder changed after bad-kernel installation",
                "expected BootOrder": boot_order_hex_str(initial_bootvars.boot_order),
                "got BootOrder": boot_order_hex_str(bad_kernel_bootvars.boot_order),
            }
        )
    if bad_kernel_bootvars.boot_current != initial_bootvars.boot_current:
        failures.append(
            {
                "msg": "BootCurrent changed after bad-kernel installation",
                "expected BootCurrent": boot_entry_hex_str(
                    initial_bootvars.boot_current
                ),
                "got BootCurrent": boot_entry_hex_str(bad_kernel_bootvars.boot_current),
            }
        )
    if bad_kernel_bootvars.boot_next == initial_bootvars.boot_next:
        failures.append(
            {
                "msg": "BootNext did not change after bad-kernel installation",
                "got BootNext": (
                    boot_entry_hex_str(bad_kernel_bootvars.boot_next)
                    if bad_kernel_bootvars.boot_next
                    else None
                ),
            }
        )
    if bad_kernel_bootvars.entries == initial_bootvars.entries:
        failures.append(
            {
                "msg": "Entries did not change after bad-kernel installation (check that bad kernel was added as an entry)",
                "got BootNext": (
                    boot_entry_hex_str(bad_kernel_bootvars.boot_next)
                    if bad_kernel_bootvars.boot_next
                    else None
                ),
            }
        )

    # If failed at this point, the rest of the test is invalid
    assert failures == [], f"Fallback mechanism failed: {pformat(failures)}"

    cvm.reboot(NULLBOOT_WAIT_TIME.seconds)
    LOGGER.info("Restart CVM OK")

    post_restart_bootvars = EFIVars.from_efibootmgr(cvm)

    if bad_kernel_bootvars.boot_order != post_restart_bootvars.boot_order:
        failures.append(
            {
                "msg": "BootOrder unexpectedly changed after reboot",
                "expected": bad_kernel_bootvars.dump(),
                "got": post_restart_bootvars.dump(),
            }
        )
    if bad_kernel_bootvars.boot_current != post_restart_bootvars.boot_current:
        failures.append(
            {
                "msg": "BootCurrent unexpectedly changed after reboot",
                "expected": bad_kernel_bootvars.dump(),
                "got": post_restart_bootvars.dump(),
            }
        )
    if post_restart_bootvars.boot_next != None:
        failures.append(
            {
                "msg": "BootNext is still set after reboot",
                "expected": None,
                "got": boot_entry_hex_str(post_restart_bootvars.boot_next),
            }
        )

    assert failures == [], f"Fallback mechanism failed: {pformat(failures)}"


def test_systemd_service(
    cvm: ConfidentialVirtualMachine,
    efi_signature: EFISignature,
    latest_kernel_version: int,
    tempdir: tempfile.TemporaryDirectory,
) -> None:
    """
    This tests the expected functionality of the systemd service in the
    nullboot package such that it will amend the BootOrder in the event
    that a known good kernel successfully boots. It verifies that the
    kernel is booted from BootNext is BootOrder[0] and BootCurrent after
    restart. Raises AssertionError if the logical portion of thetest fails.
    """
    failures = []

    initial_bootvars = EFIVars.from_efibootmgr(cvm)

    # Copy known good kernel to test it boots properly
    copy_host_kernel(cvm, latest_kernel_version)

    cvm.run_cmd(["nullbootctl"])
    LOGGER.info("Run nullbootctl OK")

    good_kernel_bootvars = EFIVars.from_efibootmgr(cvm)

    if good_kernel_bootvars.boot_order != initial_bootvars.boot_order:
        failures.append(
            {
                "msg": "BootOrder changed after new kernel installation",
                "expected BootOrder": boot_order_hex_str(initial_bootvars.boot_order),
                "got BootOrder": boot_order_hex_str(good_kernel_bootvars.boot_order),
            }
        )
    if good_kernel_bootvars.boot_current != initial_bootvars.boot_current:
        failures.append(
            {
                "msg": "BootCurrent changed after new kernel installation",
                "expected BootCurrent": boot_entry_hex_str(
                    initial_bootvars.boot_current
                ),
                "got BootCurrent": boot_entry_hex_str(
                    good_kernel_bootvars.boot_current
                ),
            }
        )

    if (
        good_kernel_bootvars.boot_next == None
        or good_kernel_bootvars.boot_next == initial_bootvars.boot_next
    ):
        failures.append(
            {
                "msg": "BootNext did not change after new kernel installation",
                "got BootNext": (
                    boot_entry_hex_str(good_kernel_bootvars.boot_next)
                    if good_kernel_bootvars.boot_next
                    else None
                ),
            }
        )

    # If failed at this point, the rest of the test is invalid
    assert failures == [], f"Failed to install new kernel: {pformat(failures)}"

    cvm.reboot(NULLBOOT_WAIT_TIME.seconds)
    LOGGER.info("Restart CVM OK")

    reboot_bootvars = EFIVars.from_efibootmgr(cvm)
    if reboot_bootvars.boot_current != good_kernel_bootvars.boot_next:
        failures.append(
            {
                "msg": "BootNext was not booted",
                "expected BootCurrent": boot_entry_hex_str(
                    good_kernel_bootvars.boot_next
                ),
                "got BootCurrent": boot_entry_hex_str(reboot_bootvars.boot_current),
            }
        )
    if reboot_bootvars.boot_current != reboot_bootvars.boot_order[0]:
        failures.append(
            {
                "msg": "BootCurrent is not the first entry in the BootOrder",
                "BootCurrent": boot_entry_hex_str(reboot_bootvars.boot_current),
                "BootOrder": boot_order_hex_str(reboot_bootvars.boot_order),
            }
        )

    assert failures == [], f"Systemd service failed: {pformat(failures)}"


def execute(args):
    """Generate the test environment and run nullboot end-to-end tests."""
    arch = util.Arch(args.arch)
    release = args.release
    packages = args.packages

    tempdir = tempfile.TemporaryDirectory("")

    package_paths = util.prepare_packages(tempdir.name, PACKAGE_SETS[arch], packages)

    # Download ECI before image to ensure NBD is loaded before setting up
    # VM infrastructure (which can be time-consuming)
    eci = EncryptCloudImage(tempdir.name)
    LOGGER.debug("Initialized encrypt-cloud-image")

    cvm_image_name = ConfidentialVirtualMachine.prepare_cvm_image(
        tempdir.name, release, arch
    )
    LOGGER.debug("Downloaded and truncated CVM Image")

    # Create virtual machine
    cvm = ConfidentialVirtualMachine(tempdir.name, None, arch, util.Firmware.UEFI)
    LOGGER.debug("Initialized VM Environment")

    efi_signature = EFISignature.x509_format(tempdir.name)
    efi_signature.register_to_vm(cvm)
    LOGGER.debug("Registered EFI Signature to VM")

    uefi_config_path = write_uefi_config(cvm)
    LOGGER.debug("Wrote UEFI Config")

    # VirtualMachine uses "disk.img" as the name of the image
    encrypted_image_name = "disk.img"
    eci.encrypt(
        image_dir=tempdir.name,
        image_name=cvm_image_name,
        encrypted_image_name=encrypted_image_name,
        cwd=tempdir.name,
    )
    LOGGER.debug("Encrypted CVM Image")

    eci.deploy(
        uefi_config_path=uefi_config_path,
        encrypted_image_name=encrypted_image_name,
        srk_path="srk.pub",
        cwd=tempdir.name,
    )
    LOGGER.debug("Deployed CVM Image")

    LOGGER.info("Begin Testing")
    cvm.start()
    LOGGER.info("Boot and provision image OK")

    install_new_packages(cvm, package_paths)
    LOGGER.info("Install new nullboot OK")

    # Rebooting at this point tests that we can still unlock the FDE key
    # boot, and connect into the CVM after nullboot installation trigger
    # is run
    cvm.reboot(NULLBOOT_WAIT_TIME.seconds)
    LOGGER.info("Shut down and reboot virtual machine OK")

    # Need kernel artifacts to be registered as the latest available kernel version
    latest_kernel_version = 99999

    # Run test
    test_recovery_key(cvm)
    LOGGER.info("Test recovery key OK")

    test_fallback_mechanism(cvm, efi_signature, latest_kernel_version, tempdir)
    LOGGER.info("Test fallback mechanism OK")

    latest_kernel_version += 1
    test_systemd_service(cvm, efi_signature, latest_kernel_version, tempdir)
    LOGGER.info("Test systemd service OK")

    cvm.shutdown()
    LOGGER.info("Shut down OK")
