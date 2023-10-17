#
# ubuntu-boot-test: vm.py: Virtual machine manager
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
import os
import pexpect
import shutil
import subprocess
import sys
import tempfile

vmconfig_uefi = {
  (Arch.AMD64, Firmware.UEFI): {
    "cmd": "qemu-system-x86_64",
    "machine": "q35",
    "cpu": "qemu64",
    "ram": "2048",
    "fw_code": "/usr/share/OVMF/OVMF_CODE.fd",
    "fw_vars_template": {
      Secboot.NONE: "/usr/share/OVMF/OVMF_VARS.fd",
      Secboot.MS: "/usr/share/OVMF/OVMF_VARS.ms.fd",
      Secboot.SNAKEOIL: "/usr/share/OVMF/OVMF_VARS.snakeoil.fd"
    }
  },
  (Arch.AMD64, Firmware.PCBIOS): {
    "cmd": "qemu-system-x86_64",
    "machine": "q35",
    "cpu": "qemu64",
    "ram": "2048"
  },
  (Arch.ARM64, Firmware.UEFI): {
    "cmd": "qemu-system-aarch64",
    "machine": "virt",
    "cpu": "cortex-a72",
    "ram": "2048",
    "fw_code": "/usr/share/AAVMF/AAVMF_CODE.fd",
    "fw_vars_template": {
      Secboot.NONE: "/usr/share/AAVMF/AAVMF_VARS.fd",
      Secboot.MS: "/usr/share/AAVMF/AAVMF_VARS.ms.fd",
      Secboot.SNAKEOIL: "/usr/share/AAVMF/AAVMF_VARS.snakeoil.fd"
    }
  }
}

METADATA = "local-hostname: ubuntu-boot-test"

USERDATA = """#cloud-config
runcmd:
  - passwd -d root
  - sed -i -e '/^#\\?PasswordAuthentication/s/^.*$/PasswordAuthentication yes/' /etc/ssh/sshd_config
  - sed -i -e '/^#\\?PermitEmptyPasswords/s/^.*$/PermitEmptyPasswords yes/' /etc/ssh/sshd_config
  - sed -i -e '/^#\\?PermitRootLogin/s/^.*$/PermitRootLogin yes/' /etc/ssh/sshd_config
  - systemctl restart sshd
  - echo CLOUD_INIT_DONE > /dev/ttyS0
"""

def create_cloud_init_seed(dest_path):
  tempdir = tempfile.TemporaryDirectory()
  metadata_path = os.path.join(tempdir.name, "meta-data")
  userdata_path = os.path.join(tempdir.name, "user-data")
  with open(metadata_path, "w") as f:
    f.write(METADATA)
  with open(userdata_path, "w") as f:
    f.write(USERDATA)
  result = subprocess.run(["genisoimage", "-joliet", "-rock", "-quiet",
    "-output", dest_path,
    "-volid", "cidata",
    metadata_path, userdata_path])
  assert result.returncode == 0

class VirtualMachine:
  def __init__(self, release, arch, firmware, secboot):
    if (arch, firmware) not in vmconfig_uefi:
      assert False, f"Unsupported configuration {arch} {firmware}"

    self._tempdir = tempfile.TemporaryDirectory()
    self._vmpath = self._tempdir.name

    self._arch = arch
    self._firmware = firmware
    self._config = vmconfig_uefi[(arch, firmware)]

    if self._firmware == Firmware.UEFI:
      self._uefi_vars_path = os.path.join(self._vmpath, "UEFI_VARS.fd")
      shutil.copy(self._config["fw_vars_template"][secboot], self._uefi_vars_path)

    self._disk_image_path = os.path.join(self._vmpath, "disk.img")
    download_image(release, arch, self._disk_image_path)

    self._cloud_init_seed_path = os.path.join(self._vmpath, "cloud-init.seed")
    create_cloud_init_seed(self._cloud_init_seed_path)

    self._ssh_port = 2222
    self._firstboot = True

    if DEBUG:
      print(f"Created VM directory {self._vmpath}")
      print(f"Using QEMU {self._config['cmd']}")
      print(f"Using QEMU machine type {self._config['machine']}")
      if self._firmware == Firmware.UEFI:
        print(f"Using code firmware volume {self._config['fw_code']}")
        print(f"Created variable firmware volume {self._uefi_vars_path}")
      print(f"Created disk image {self._disk_image_path}")
      print(f"Created cloud-init seed {self._cloud_init_seed_path}")

  def start(self):
    base_opts = [self._config["cmd"],
      "-m", self._config["ram"],
      "-M", f"{self._config['machine']},accel={accel_for(self._arch)}",
      "-cpu", f"{self._config['cpu']}",
      "-serial", "mon:stdio",
      "-nographic",
      "-netdev", f"user,id=net0,hostfwd=tcp::{self._ssh_port}-:22",
      "-device", "virtio-net-pci,romfile=,netdev=net0"
    ]

    if self._firmware == Firmware.UEFI:
      fw_opts = [
        "-drive", f"file={self._config['fw_code']},if=pflash,format=raw,unit=0,readonly=on",
        "-drive", f"file={self._uefi_vars_path},if=pflash,format=raw,unit=1"
      ]
    else:
      fw_opts = []

    disk_opts = [
      "-drive", f"file={self._disk_image_path},if=none,id=hdd",
      "-device", f"virtio-blk-pci,drive=hdd,bootindex=0",
      "-drive", f"file={self._cloud_init_seed_path},if=virtio,format=raw,readonly=on"
    ]

    # Create VM process
    self._vm_process = pexpect.spawn(" ".join(base_opts + fw_opts + disk_opts))

  def waitboot(self):
    if self._firstboot:
      self._firstboot = False
      self._vm_process.expect("CLOUD_INIT_DONE", timeout=1800)
    self._vm_process.expect("ubuntu-boot-test login: ", timeout=1800)

  def waitshutdown(self):
    self._vm_process.wait()

  def run_cmd(self, args):
    result = subprocess.run(["ssh",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-p", f"{self._ssh_port}",
      "root@localhost" ] + args)
    assert result.returncode == 0

  def copy_files(self, src_file_paths, dest_dir_path):
    result = subprocess.run(["scp",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-P", f"{self._ssh_port}"] +
      src_file_paths +
      [f"root@localhost:{dest_dir_path}"])
    assert result.returncode == 0


