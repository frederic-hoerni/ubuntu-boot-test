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

from enum import Enum
from OpenSSL import crypto
from ubuntu_boot_test.config import *
import binascii
import gzip
import os
import requests
import shutil
import struct
import subprocess
import sys


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

def kvm_supported(guest_arch):
  # Accelaration can be used when the guest has the same architecture as the
  # host kernel (NOTE: this is a simplification but it's good enough here)
  # We also need to ensure that the KVM device node is exposed and accessible
  if host_kernel_arch() == guest_arch and \
      os.access("/dev/kvm", os.R_OK | os.W_OK):
    return True
  return False

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

def validate_packages(expected_package_set, package_paths):
  actual_package_set = set()

  for package_path in package_paths:
    _, filename = os.path.split(package_path)
    actual_package_set.add(filename.split("_")[0])

  if actual_package_set != expected_package_set:
    print("Invalid package set provided " \
          f"(actual {actual_package_set}, expecting {expected_package_set})",
          file=sys.stderr)
    return False

  return True

def download_packages(packages, dest_path):
  result = subprocess.run(["apt-get", "download"] + list(packages),
    cwd=dest_path,
    capture_output=not DEBUG)
  assert result.returncode == 0, "Failed to download packages"

def prepare_packages(tempdir_path, expected_package_set, cmdline_package_paths):
  if len(cmdline_package_paths) > 0:
    # Validate package set
    if not validate_packages(expected_package_set, cmdline_package_paths):
      exit(1)
    # Copy packages to tempdir
    for package_path in cmdline_package_paths:
      shutil.copy(package_path,
        os.path.join(tempdir_path, os.path.basename(package_path)))
  else:
    # Download packages
    download_packages(expected_package_set, tempdir_path)

  # List package file paths
  return [
    os.path.join(tempdir_path, package_filename)
    for package_filename in os.listdir(tempdir_path)
    if package_filename.endswith("deb")
  ]

EFI_CERT_X509_GUID = bytes((
  0xa1, 0x59, 0xc0, 0xa5, 0xe4, 0x94, 0xa7, 0x4a,
  0x87, 0xb5, 0xab, 0x15, 0x5c, 0x2b, 0xf0, 0x72))

EFI_CERT_SHA256_GUID = bytes((
  0x26, 0x16, 0xc4, 0xc1, 0x4c, 0x50, 0x92, 0x40,
  0xac, 0xa9, 0x41, 0xf9, 0x36, 0x93, 0x43, 0x28))

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

def pe_to_efihash(pe_path):
  # Use pesign to get the authenticode hash of the PE
  # This pulls in pesign as yet another dependency, maybe we want to use:
  # https://git.launchpad.net/~mkukri/+git/sbsigntool/tree/src/sbhash.c
  result = subprocess.run(["pesign", "-h", "-i", pe_path], capture_output=True)
  assert result.returncode == 0
  digest = binascii.unhexlify(result.stdout.decode().split(" ")[0])
  assert len(digest) == 32

  # Create ESL
  signature_list_size = 16 + 4 + 4 + 4 + \
                          16 + len(digest)
  signature_header_size = 0
  signature_size = 16 + len(digest)
  return EFI_CERT_SHA256_GUID + \
         struct.pack("<III", signature_list_size,
                              signature_header_size,
                              signature_size) + \
         SIGNATURE_OWNER_GUID + digest

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

def is_uefica_signed(path):
  uefica_dir = os.path.join(os.path.dirname(__file__), "uefica")
  for filename in os.listdir(uefica_dir):
    uefica_path = os.path.join(uefica_dir, filename)
    result = subprocess.run(["sbverify", "--cert", uefica_path, path],
                            capture_output=not DEBUG)
    if result.returncode == 0:
      return True
  return False

def is_canonical_signed(path):
  canonical_uefi_ca_path = os.path.join(os.path.dirname(__file__), "canonical-uefi-ca.pem")
  result = subprocess.run(["sbverify", "--cert", canonical_uefi_ca_path, path], capture_output=not DEBUG)
  return result.returncode == 0

def maybe_gzip_ctx(path):
  class GzipCtx:
    def __init__(self, compressed_path):
      self._compressed_path = compressed_path
      self._uncompressed_path = f"{compressed_path}.ungz"
      with open(self._compressed_path, "rb") as compressed_file:
        with open(self._uncompressed_path, "wb") as uncompressed_file:
          uncompressed_file.write(gzip.decompress(compressed_file.read()))
      self.path = self._uncompressed_path
    def __enter__(self):
      return self
    def __exit__(self, type, value, traceback):
      with open(self._uncompressed_path, "rb") as uncompressed_file:
        with open(self._compressed_path, "wb") as compressed_file:
          compressed_file.write(gzip.compress(uncompressed_file.read()))

  class NullCtx:
    def __init__(self, path):
      self.path = path
    def __enter__(self):
      return self
    def __exit__(self, type, value, traceback):
      pass

  try:
    return GzipCtx(path)
  except:
    return NullCtx(path)

def deb_repack_ctx(tempdir, path):
  class DebRepackCtx:
    def __init__(self, path):
      assert path[-4:] == ".deb"
      self._deb_path = path
      self.dir_path = os.path.join(tempdir, os.path.basename(path)[:-4])
      result = subprocess.run(["dpkg-deb", "-R", self._deb_path, self.dir_path],
        capture_output=not DEBUG)
      assert result.returncode == 0
    def __enter__(self):
      return self
    def __exit__(self, type, value, traceback):
      # Suffix version with "+r1"
      with open(os.path.join(self.dir_path, "DEBIAN", "control"), "rb+") as cfile:
        lines = cfile.read().split(b"\n")
        for idx in range(len(lines)):
          if lines[idx].startswith(b"Version: "):
            lines[idx] = lines[idx].strip() + b"+r1"
        cfile.seek(0)
        cfile.write(b"\n".join(lines))
      # Repack deb
      result = subprocess.run(["dpkg-deb", "-b", self.dir_path, self._deb_path],
        capture_output=not DEBUG)
      assert result.returncode == 0
  return DebRepackCtx(path)

class SbatEntry:
  def __init__(self, line):
    cols = map(lambda col: col.strip(), line.split(b","))
    self.cnam = next(cols).decode()
    self.cgen = int(next(cols))
    self.vnam = next(cols).decode()
    self.pnam = next(cols).decode()
    self.vver = next(cols).decode()
    self.vurl = next(cols).decode()

class SbatSection:
  SBAT_SECTION_HDR = b"sbat,1,SBAT Version,sbat,1,https://github.com/rhboot/shim/blob/main/SBAT.md"

  def __init__(self, data: bytes):
    lines = data.split(b"\n")
    assert lines[0] == self.SBAT_SECTION_HDR
    self.entries = {}
    for line in lines[1:]:
      try:
        ent = SbatEntry(line)
        self.entries[ent.cnam] = ent
      except:
        continue

  def get_level_for(self, cnam: str) -> int:
    return self.entries[cnam].cgen

def decode_image_sbat(tempdir_path: str, path: str) -> SbatSection:
  # Extract .sbat section from binary
  result = subprocess.run(["objcopy",
                           path,
                           os.path.join(tempdir_path, "unused.efi"),
                           "--dump-section",
                           ".sbat=/dev/stdout"], capture_output=True)
  assert result.returncode == 0

  # Parse .sbat section
  return SbatSection(result.stdout)

class SbatLevel:
  def __init__(self, data: bytes):
    lines = data.split(b"\n")
    assert lines[0].startswith(b"sbat,1,")
    self.date = lines[0][7:].decode()
    self.entries = {}
    for line in lines[1:]:
      try:
        cnam, cgen = map(lambda col: col.strip(), line.split(b","))
        self.entries[cnam.decode()] = int(cgen)
      except:
        continue

  def encode(self) -> bytes:
    result = bytearray()
    result.extend(f"sbat,1,{self.date}\n".encode())
    for cnam, cgen in self.entries.items():
      result.extend(f"{cnam},{cgen}\n".encode())
    return bytes(result)

  def get_level_for(self, cnam: str) -> int:
    return self.entries[cnam]

  def set_level_for(self, cnam: str, cgen: int):
    self.entries[cnam] = cgen
