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

from pexpect import fdpexpect
from pyuefivars import UEFIVar
from pyuefivars.edk2 import EDK2UEFIVarStore
from ubuntu_boot_test.config import *
from ubuntu_boot_test.util import *
import atexit
import binascii
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile

# VM configuration based on architecture and firmware type
vmconfig_uefi = {
  (Arch.AMD64, Firmware.UEFI): {
    "cmd": "qemu-system-x86_64",
    "machine": "q35",
    "cpu": "qemu64",
    "ram": "512",
    "fw_code": "/usr/share/OVMF/OVMF_CODE_4M.secboot.fd",
    "fw_vars_template": {
      False: "/usr/share/OVMF/OVMF_VARS_4M.fd",
      True: "/usr/share/OVMF/OVMF_VARS_4M.ms.fd"
    }
  },
  (Arch.AMD64, Firmware.PCBIOS): {
    "cmd": "qemu-system-x86_64",
    "machine": "q35",
    "cpu": "qemu64",
    "ram": "512"
  },
  (Arch.ARM64, Firmware.UEFI): {
    "cmd": "qemu-system-aarch64",
    "machine": "virt",
    "cpu": "cortex-a72",
    "ram": "512",
    "fw_code": "/usr/share/AAVMF/AAVMF_CODE.fd",
    "fw_vars_template": {
      False: "/usr/share/AAVMF/AAVMF_VARS.fd",
      True: "/usr/share/AAVMF/AAVMF_VARS.ms.fd"
    }
  }
}

# If the VM doesn't reach a desired state from boot in half an hour, then error out
vm_waittimeout = 1800

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
  cidata_path = os.path.join(os.path.dirname(__file__), "cidata")
  result = subprocess.run(["genisoimage", "-joliet", "-rock", "-quiet",
    "-output", dest_path, "-volid", "cidata", cidata_path],
    capture_output=not DEBUG)
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
    self._host_vsock.settimeout(vm_waittimeout)

    self._monitor_socket = os.path.join(self._vmpath, "monitor")
    self._serial_socket = os.path.join(self._vmpath, "serial")

    if DEBUG:
      print(f"Created VM directory {self._vmpath}")
      print(f"Using QEMU {self._config['cmd']}")
      print(f"Using QEMU machine type {self._config['machine']}")
      if self._firmware == Firmware.UEFI:
        print(f"Using code firmware volume {self._config['fw_code']}")
        print(f"Created variable firmware volume {self._uefi_vars_path}")
      print(f"Created disk image {self._disk_image_path}")
      print(f"Created cloud-init seed {self._cloud_init_seed_path}")

  def dump_vars(self):
    # Read current variable store
    with open(self._uefi_vars_path, "rb") as f:
      vars_data = f.read()
    # Find existing variable
    varstore = EDK2UEFIVarStore(vars_data)
    for var in varstore.vars:
      print(f"{binascii.hexlify(var.guid)}:{var.name} = {binascii.hexlify(var.data)}")

  def write_efivar(self, guid, name, data, append=True):
    # Read current variable store
    with open(self._uefi_vars_path, "rb") as f:
      vars_data = f.read()
      vars_length = f.tell()
    # Find existing variable
    varstore = EDK2UEFIVarStore(vars_data)
    found = False
    for var in varstore.vars:
      if var.guid == guid and var.name == name:
        if append:
          var.data += data
        else:
          var.data = data
        found = True
        break
    # Create new variable
    if not found:
      varstore.vars.append(UEFIVar(name, data, guid, 3))
    # Dump variable store back out
    with open(self._uefi_vars_path, "wb") as f:
      # FIXME: this is a workaround the incorrect length
      # value used by the bytes() function of EDK2UEFIVarStore
      # This means that the original file length gets lost and
      # we must manually pad the thing
      varstore.filelen = varstore.length
      f.write(bytes(varstore))
      if f.tell() < vars_length:
        f.write(b"\xff" * (vars_length - f.tell()))

  def start(self, ephemeral_snapshot=False, wait=True):
    serialcon = f"unix:{self._serial_socket},server,nowait"
    if DEBUG:
      serialcon += ",logfile=/dev/tty"

    if kvm_supported(self._arch):
      accel = "kvm"
      cpu = "host"
    else:
      accel = "tcg"
      cpu = self._config["cpu"]

    opts = [self._config["cmd"],
      "-m", self._config["ram"],
      "-M", f"{self._config['machine']},accel={accel}",
      "-cpu", cpu,
      "-nographic",
      "-monitor", f"unix:{self._monitor_socket},server,nowait",
      "-serial", serialcon,
      "-device", "vhost-vsock-pci,guest-cid=3",
      "-netdev", "user,id=net0,hostfwd=tcp::2222-:22",
      "-device", "virtio-net-pci,romfile=,netdev=net0"
    ]

    if self._firmware == Firmware.UEFI:
      opts += [
        "-drive", f"file={self._config['fw_code']},if=pflash,format=raw,unit=0,readonly=on",
        "-drive", f"file={self._uefi_vars_path},if=pflash,format=raw,unit=1"
      ]

    opts += [
      "-drive", f"file={self._disk_image_path},if=none,id=hdd",
      "-device", f"virtio-blk-pci,drive=hdd,serial=0,bootindex=0",
      "-drive", f"file={self._cloud_init_seed_path},if=virtio,format=raw,readonly=on",
    ]

    if ephemeral_snapshot:
      opts.append("-snapshot")

    # Create VM process
    self._vm_process = subprocess.Popen(opts)

    # Make sure VM exits if we error out
    def kill_vm_process():
      try:
        os.kill(self._vm_process.pid, signal.SIGINT)
      except:
        pass

    atexit.register(kill_vm_process)

    # Wait for boot if requested
    if wait:
      self.waitboot()

  def reboot(self, wait=True):
    self.run_cmd(["reboot"], assert_ok=False)
    if wait:
      self.waitboot()

  def shutdown(self, wait=True):
    self.run_cmd(["poweroff"], assert_ok=False)
    if wait:
      self.waitshutdown()

  def forceshutdown(self, wait=True):
    os.kill(self._vm_process.pid, signal.SIGINT)
    if wait:
      self.waitshutdown()

  def waitboot(self):
    connfd, _ = self._host_vsock.accept()
    assert connfd.recv(3) == b"RDY"
    connfd.close()

  def waitserial(self, data):
    # Wait for serial to come up
    while not os.access(self._serial_socket, os.F_OK):
      pass
    # Wait for data
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as serfd:
      serfd.connect(self._serial_socket)
      serp = fdpexpect.fdspawn(serfd, timeout=vm_waittimeout)
      serp.expect(re.escape(data))

  def waitshutdown(self):
    self._vm_process.wait()

  def run_cmd(self, args, assert_ok=True):
    result = subprocess.run(["ssh",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-p", "2222",
      "root@localhost" ] + args, capture_output=True)
    if DEBUG:
      print(result.stdout.decode())
      print(result.stderr.decode(), file=sys.stderr)
    if assert_ok:
      assert result.returncode == 0
    return result.stdout.decode()

  def copy_files(self, src_file_paths, dest_dir_path):
    result = subprocess.run(["scp",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-P", "2222"] +
      src_file_paths +
      [f"root@localhost:{dest_dir_path}"], capture_output=not DEBUG)
    assert result.returncode == 0

  def get_files(self, src_file_paths, dest_dir_path):
    result = subprocess.run(["scp",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-P", "2222"] +
      [ f"root@localhost:{src_file_path}" for src_file_path in src_file_paths ] +
      [ dest_dir_path ], capture_output=not DEBUG)
    assert result.returncode == 0

  def remote_file(self, remote_path):
    class RemoteFileContext:
      """Context manager for modifying remote files
      """
      def __init__(self, vm, remote_path, tempdir_path):
        self._vm = vm
        self._remote_path = remote_path
        self.local_path = os.path.join(
          tempdir_path, os.path.split(remote_path)[-1])
        self._vm.get_files([self._remote_path], self.local_path)

      def __enter__(self):
        return self

      def __exit__(self, type, value, traceback):
        self._vm.copy_files([self.local_path], self._remote_path)

    return RemoteFileContext(self, remote_path, self._vmpath)

  def disablesb(self):
    self.write_efivar(SECURE_BOOT_ENABLE_GUID, "SecureBootEnable", b"\x00", append=False)

  def enablesb(self):
    self.write_efivar(SECURE_BOOT_ENABLE_GUID, "SecureBootEnable", b"\x01", append=False)
