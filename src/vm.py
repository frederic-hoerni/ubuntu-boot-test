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
from pyuefivars.edk2 import EDK2UEFIVarStore
import atexit
import os
import shutil
import signal
import socket
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

def ensure_vsock_accessible():
  lsmod_result = subprocess.run(["lsmod"], stdout=subprocess.PIPE)
  if b"vhost_vsock" not in lsmod_result.stdout:
    if os.getuid() != 0:
      assert False, "Need root to load vhost_vsock"
    if subprocess.run(["modprobe", "vhost_vsock"]).returncode != 0:
      assert False, "Failed to load vhost_vsock"

  if not os.access("/dev/vhost-vsock", os.R_OK | os.W_OK):
    assert False, "/dev/vhost-vsock is not accessible"

def create_cloud_init_seed(dest_path):
  cidata_path = os.path.join(os.path.dirname(sys.argv[0]), "cidata")
  result = subprocess.run(["genisoimage", "-joliet", "-rock", "-quiet",
    "-output", dest_path, "-volid", "cidata", cidata_path])
  assert result.returncode == 0

class VirtualMachine:
  def __init__(self, release, arch, firmware, secboot):
    if (arch, firmware) not in vmconfig_uefi:
      assert False, f"Unsupported configuration {arch} {firmware}"

    ensure_vsock_accessible()

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

    self._host_vsock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    self._host_vsock.bind((socket.VMADDR_CID_HOST, 4444))
    self._host_vsock.listen(10)

    if DEBUG:
      print(f"Created VM directory {self._vmpath}")
      print(f"Using QEMU {self._config['cmd']}")
      print(f"Using QEMU machine type {self._config['machine']}")
      if self._firmware == Firmware.UEFI:
        print(f"Using code firmware volume {self._config['fw_code']}")
        print(f"Created variable firmware volume {self._uefi_vars_path}")
      print(f"Created disk image {self._disk_image_path}")
      print(f"Created cloud-init seed {self._cloud_init_seed_path}")

  def start(self, ephermal_snapshot=False):
    base_opts = [self._config["cmd"],
      "-m", self._config["ram"],
      "-M", f"{self._config['machine']},accel={accel_for(self._arch)}",
      "-cpu", f"{self._config['cpu']}",
      "-nographic",
      "-monitor", "none",
      "-serial", "file:/dev/tty" if DEBUG else "none",
      "-device", "vhost-vsock-pci,guest-cid=3",
      "-netdev", "user,id=net0,hostfwd=tcp::2222-:22",
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
      "-drive", f"file={self._cloud_init_seed_path},if=virtio,format=raw,readonly=on",
    ]

    if ephermal_snapshot:
      disk_opts.append("-snapshot")

    # Create VM process
    self._vm_process = subprocess.Popen(base_opts + fw_opts + disk_opts)

    # Make sure VM exits if we error out
    def kill_vm_process():
      try:
        os.kill(self._vm_process.pid, signal.SIGINT)
      except:
        pass
    atexit.register(kill_vm_process)

  def waitboot(self):
    connfd, _ = self._host_vsock.accept()
    assert connfd.recv(3) == b"RDY"
    connfd.close()

  def waitshutdown(self):
    self._vm_process.wait()

  def run_cmd(self, args, assert_ok=True):
    result = subprocess.run(["ssh",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-p", "2222",
      "root@localhost" ] + args)
    if assert_ok:
      assert result.returncode == 0

  def copy_files(self, src_file_paths, dest_dir_path):
    result = subprocess.run(["scp",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-P", "2222"] +
      src_file_paths +
      [f"root@localhost:{dest_dir_path}"])
    assert result.returncode == 0
