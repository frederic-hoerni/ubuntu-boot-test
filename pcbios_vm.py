#
# ubuntu-boot-test: pcbios.py: BIOS virtual machine manager
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

vmconfig = {
  Arch.AMD64: {
    "cmd": "qemu-system-x86_64",
    "machine": "q35",
    "cpu": "qemu64",
    "ram": "2048"
  },
}

def accel_for(guest_arch):
  # Accelaration can be used when the guest has the same architecture as the
  # host kernel (NOTE: this is a simplification but it's good enough here)
  # We also need to ensure that the KVM device node is exposed and accessible
  if Arch.host_kernel_arch() == guest_arch and \
      os.access("/dev/kvm", os.R_OK | os.W_OK):
    return "kvm"

  # Otherwise we fall back to JIT recompilation using TCG
  return "tcg"

def download_image(release, arch, dest_path):
  arch = { Arch.AMD64: "amd64" }[arch]

  if LOCAL_IMG_SERVER:
    src_url = f"http://localhost/cloudimg/{release}-server-cloudimg-{arch}.img"
  else:
    src_url = f"http://cloud-images.ubuntu.com/{release}/current/{release}-server-cloudimg-{arch}.img"

  with open(dest_path, "wb") as f:
    resp = requests.get(src_url)
    assert resp.status_code == 200, "Failed to download image"
    for chunk in resp.iter_content(chunk_size=4096):
      f.write(chunk)

class PCBIOSVm:
  def __init__(self, release, arch, package_path):
    if arch not in vmconfig:
      assert False, f"Unsupported architecture {arch}"

    self._tempdir = tempfile.TemporaryDirectory()
    self._vmpath = self._tempdir.name

    self._arch = arch
    self._config = vmconfig[arch]
    self._disk_image_path = os.path.join(self._vmpath, "disk.img")
    download_image(release, arch, self._disk_image_path)

    if DEBUG:
      print(f"Created VM directory              {self._vmpath}")
      print(f"Using QEMU                        {self._config['cmd']}")
      print(f"Using QEMU machine type           {self._config['machine']}")
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
    cmd = " ".join([self._config["cmd"],
      "-m", self._config["ram"],
      "-M", f"{self._config['machine']},accel={accel_for(self._arch)}",
      "-cpu", f"{self._config['cpu']}",
      "-serial", "mon:stdio",
      "-nographic",
      "-drive", f"file={self._disk_image_path},if=none,id=hdd",
      "-device", f"virtio-blk-pci,drive=hdd,bootindex=0",
      "-drive", f"file={self._cloud_init_seed_path},if=virtio,format=raw,readonly=on"])
    proc = pexpect.spawn(cmd)
    proc.timeout = 300
    if DEBUG:
      proc.logfile = sys.stdout.buffer
    handler(proc)
    return proc.wait()
