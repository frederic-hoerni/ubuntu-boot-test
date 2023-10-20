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
from OpenSSL import crypto
import os
import requests
import struct
import subprocess
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

class Secboot(Enum):
  """Secure Boot configuration
  """
  NONE      = "none"
  SIGN      = "sign"
  MS        = "ms"

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

EFI_CERT_X509_GUID = bytes((
  0xa1, 0x59, 0xc0, 0xa5, 0xe4, 0x94, 0xa7, 0x4a,
  0x87, 0xb5, 0xab, 0x15, 0x5c, 0x2b, 0xf0, 0x72))

EFI_GLOBAL_VARIABLE_GUID = bytes((
  0x61, 0xdf, 0xe4, 0x8b, 0xca, 0x93, 0xd2, 0x11,
  0xaa, 0x0d, 0x00, 0xe0, 0x98, 0x03, 0x2b, 0x8c))

EFI_IMAGE_SECURITY_DATABASE_GUID = bytes((
  0xcb, 0xb2, 0x19, 0xd7, 0x3a, 0x3d, 0x96, 0x45,
  0xa3, 0xbc, 0xda, 0xd0, 0x0e, 0x67, 0x65, 0x6f))

SECURE_BOOT_ENABLE_GUID = bytes((
  0xc7, 0x0b, 0xa3, 0xf0, 0x08, 0xaf, 0x56, 0x45,
  0x99, 0xc4, 0x00, 0x10, 0x09, 0xc9, 0x3a, 0x44))

SHIM_LOCK_GUID = bytes((
  0x50, 0xab, 0x5d, 0x60, 0x46, 0xe0, 0x00, 0x43,
  0xab, 0xb6, 0x3d, 0xd8, 0x10, 0xdd, 0x8b, 0x23))

SIGNATURE_OWNER_GUID = bytes((
  0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42,
  0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42))

def asn1_to_efi(asn1_data):
  signature_list_size = 16 + 4 + 4 + 4 + \
                          16 + len(asn1_data)
  signature_header_size = 0
  signature_size = 16 + len(asn1_data)
  return EFI_CERT_X509_GUID + \
         struct.pack("<III", signature_list_size,
                              signature_header_size,
                              signature_size) + \
         SIGNATURE_OWNER_GUID + asn1_data

def gen_efi_signkey():
  """Generate an X509 signing key and certificate
  """
  key = crypto.PKey()
  key.generate_key(crypto.TYPE_RSA, 4096)

  pem_private_key = crypto.dump_privatekey(crypto.FILETYPE_PEM, key)

  cert = crypto.X509()
  cert.get_subject().CN = "ubuntu-boot-test"
  cert.set_serial_number(42)
  cert.gmtime_adj_notBefore(0)            # start now
  cert.gmtime_adj_notAfter(24 * 60 * 60)  # 1 day from now
  cert.set_issuer(cert.get_subject())
  cert.set_pubkey(key)
  cert.sign(key, "sha256")

  pem_certificate = crypto.dump_certificate(
    crypto.FILETYPE_PEM, cert)
  esl_certificate = asn1_to_efi(
    crypto.dump_certificate(crypto.FILETYPE_ASN1, cert))

  return (pem_private_key, pem_certificate, esl_certificate)

