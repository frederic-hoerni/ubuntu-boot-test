#
# ubuntu-boot-test: uefi_vm.py: UEFI virtual machine manager
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

import os
import pexpect
import requests
import shutil
import subprocess
import sys
import tempfile
from config import *

def qemu_cmd(arch):
  if arch == "amd64":
    return "qemu-system-x86_64"
  if arch == "aarch64":
    return "qemu-system-aarch64"
  assert False, f"Invalid architecture {arch}"

def qemu_machine(arch):
  if arch == "amd64":
    return "q35"
  if arch == "aarch64":
    return "virt"
  assert False, f"Invalid architecture {arch}"

def qemu_cpu(arch):
  if arch == "amd64":
    return "qemu64"
  if arch == "aarch64":
    return "virt"
  assert False, f"Invalid architecture {arch}"

def uefi_code_path(arch):
  if arch == "amd64":
    return f"/usr/share/OVMF/OVMF_CODE.fd"
  if arch == "aarch64":
    return f"/usr/share/AAVMF/AAVMF_CODE.fd"

  assert False, f"Invalid architecture {arch}"

def uefi_vars_path(arch):
  if arch == "amd64":
    return f"/usr/share/OVMF/OVMF_VARS.fd"
  if arch == "aarch64":
    return f"/usr/share/AAVMF/AAVMF_VARS.fd"

  assert False, f"Invalid architecture {arch}"

def cloudimg_url(release, arch):
  if arch == "aarch64": # cloudimg URLs use arm64 over aarch64
    arch = "arm64"

  if DEBUG:
    return f"http://localhost/cloudimg/{release}-server-cloudimg-{arch}.img"

  return f"http://cloud-images.ubuntu.com/{release}/current/{release}-server-cloudimg-{arch}.img"

def download_file(src_url, dest_path):
  with open(dest_path, "wb") as f:
    resp = requests.get(src_url)
    assert resp.status_code == 200
    for chunk in resp.iter_content(chunk_size=4096):
      f.write(chunk)

class UEFIVm:
  def __init__(self, release, arch, package_path):
    self._qemu_cmd = qemu_cmd(arch)
    self._qemu_machine = qemu_machine(arch)
    self._qemu_cpu = qemu_cpu(arch)

    self._tempdir = tempfile.TemporaryDirectory()
    self._vmpath = self._tempdir.name

    if DEBUG:
      print(f"Using QEMU                        {self._qemu_cmd}")
      print(f"Using QEMU machine type           {self._qemu_machine}")
      print(f"Created VM directory              {self._vmpath}")

    self._uefi_code_path = uefi_code_path(arch)
    self._uefi_vars_path = os.path.join(self._vmpath, "UEFI_VARS.fd")
    shutil.copy(uefi_vars_path(arch), self._uefi_vars_path)

    if DEBUG:
      print(f"Using code firmware volume        {self._uefi_code_path}")
      print(f"Created variable firmware volume  {self._uefi_vars_path}")

    self._disk_image_path = os.path.join(self._vmpath, "disk.img")
    download_file(cloudimg_url(release, arch), self._disk_image_path)

    if DEBUG:
      print(f"Created disk image                {self._disk_image_path}")

    self._create_cloud_init_seed(package_path)

  def _create_cloud_init_seed(self, package_path):
    with open(os.path.join(self._vmpath, "meta-data"), "w") as f:
      f.write(METADATA)
    with open(os.path.join(self._vmpath, "user-data"), "w") as f:
      f.write(USERDATA)

    package_files = map(
      lambda file: os.path.join(package_path, file),
      os.listdir(package_path))

    subprocess.run(["genisoimage",
      "-output", "cloud-init.seed", "-volid", CILABEL, "-joliet", "-rock",
      "-quiet", "user-data", "meta-data"] + list(package_files),
       cwd=self._vmpath)

    self._cloud_init_seed_path = os.path.join(self._vmpath, "cloud-init.seed")

    if DEBUG:
      print(f"Created cloud-init seed           {self._cloud_init_seed_path}")

  def run(self, handler):
    cmd = " ".join([self._qemu_cmd,
      "-m", "2048",
      "-M", f"{self._qemu_machine},accel=kvm",
      "-cpu", f"{self._qemu_cpu}",
      "-serial", "mon:stdio",
      "-nographic",
      "-drive", f"file={self._uefi_code_path},if=pflash,format=raw,unit=0,readonly=on",
      "-drive", f"file={self._uefi_vars_path},if=pflash,format=raw,unit=1",
      "-drive", f"file={self._disk_image_path},if=none,id=hdd",
      "-device", f"virtio-blk-pci,drive=hdd,bootindex=0",
      "-drive", f"file={self._cloud_init_seed_path},if=virtio,format=raw,readonly=on"])
    proc = pexpect.spawn(cmd)
    proc.timeout = 300
    if DEBUG:
      proc.logfile = sys.stdout.buffer
    handler(proc)
    return proc.wait()
