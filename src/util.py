#
# ubuntu-boot-test: util.py: Utility classes and functions
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
from enum import Enum
import os
import subprocess
import requests
import tempfile

class Arch(Enum):
  """Processor architecture
  """
  AMD64   = "amd64"
  ARM64   = "arm64"

  def __str__(self):
    return self.value

class Firmware(Enum):
  """Firmware type
  """
  UEFI    = "uefi"
  PCBIOS  = "pcbios"

  def __str__(self):
    return self.value

def host_kernel_arch():
  """Determine the host's kernel architecture
  """
  machine = os.uname().machine
  if machine == "x86_64":
    return Arch.AMD64
  if machine == "aarch64":
    return Arch.ARM64
  assert False, f"Unknown kernel architecture {machine}"

def accel_for(guest_arch):
  # Accelaration can be used when the guest has the same architecture as the
  # host kernel (NOTE: this is a simplification but it's good enough here)
  # We also need to ensure that the KVM device node is exposed and accessible
  if host_kernel_arch() == guest_arch and \
      os.access("/dev/kvm", os.R_OK | os.W_OK):
    return "kvm"

  # Otherwise we fall back to JIT recompilation using TCG
  return "tcg"

def download_image(release, arch, dest_path):
  arch = { Arch.AMD64: "amd64",
           Arch.ARM64: "arm64" }[arch]

  if LOCAL_IMG_SERVER:
    src_url = f"http://localhost/cloudimg/{release}-server-cloudimg-{arch}.img"
  else:
    src_url = f"http://cloud-images.ubuntu.com/{release}/current/{release}-server-cloudimg-{arch}.img"

  with open(dest_path, "wb") as f:
    resp = requests.get(src_url)
    assert resp.status_code == 200, "Failed to download image"
    for chunk in resp.iter_content(chunk_size=4096):
      f.write(chunk)

def download_packages(packages, dest_path):
  result = subprocess.run(
    ["apt-get", "download"] + list(packages),
    cwd=dest_path)
  assert result.returncode == 0, "Failed to download packages"


def create_cloud_init_seed(package_dir_path, dest_path):
  tempdir = tempfile.TemporaryDirectory()
  metadata_path = os.path.join(tempdir.name, "meta-data")
  userdata_path = os.path.join(tempdir.name, "user-data")
  with open(metadata_path, "w") as f:
    f.write(METADATA)
  with open(userdata_path, "w") as f:
    f.write(USERDATA)

  package_paths = map(
    lambda file: os.path.join(package_dir_path, file),
    os.listdir(package_dir_path))

  result = subprocess.run(["genisoimage", "-joliet", "-rock", "-quiet",
    "-output", dest_path,
    "-volid", CILABEL,
    metadata_path, userdata_path] + list(package_paths))
  assert result.returncode == 0
