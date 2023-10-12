#
# ubuntu-boot-test: config.py: Configuration
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
from enum import Enum

class Arch(Enum):
  """The goal of this enum is to normalize the naming convention
     used in different places for different architectures
  """
  UNK = 0
  AMD64 = 1
  ARM64 = 2

  def host_kernel_arch():
    """Determine the host's kernel architecture
    """
    machine = os.uname().machine
    if machine == "x86_64":
      return Arch.AMD64
    if machine == "aarch64":
      return Arch.ARM64
    return Arch.UNK

  def host_dpkg_arch():
    """Determine the host's dpkg architecture
    """
    arch = os.popen("dpkg --print-architecture").read()
    if arch == "amd64":
      return Arch.AMD64
    if arch == "arm64":
      return Arch.ARM64
    return Arch.UNK

# Enable debug mode
DEBUG = False

# Use local webserver for downloading images
LOCAL_IMG_SERVER = True

# Ubuntu release to use
RELEASE = "jammy"

# Architecture to use
ARCH = Arch.AMD64

# Virtual machine configuration
HOSTNAME = "ubuntu-boot-test"
USERNAME = "root"
PASSWORD = "root"

CILABEL = "cidata"

METADATA = f"local-hostname: {HOSTNAME}"

USERDATA = f"""#cloud-config
users:
  - name: {USERNAME}
    plain_text_passwd: {PASSWORD}
    lock_passwd: false
passwd: {{ expire: False }}
"""
