#
# ubuntu-boot-test: net.py: Netboot testing support code
#
# Copyright (C) 2024 Canonical, Ltd.
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

from ubuntu_boot_test.config import *
from ubuntu_boot_test.util import *
import atexit
import os
import signal
import socket
import subprocess

num_bridges = 0
num_taps = 0
v4prefixes = set()
v6prefixes = set()

def next_bridge_name():
  """Generate a likely unused bridge device name
  """
  global num_bridges
  no_bridge = num_bridges
  num_bridges += 1
  return f"ubt-br{no_bridge}"

def next_tap_name():
  """Generate a likely unused tap device name
  """
  global num_taps
  no_tap = num_taps
  num_taps += 1
  return f"ubt-tap{no_tap}"

class InetAddr:
  def __init__(self, family, addr, mask_bits):
    self.family = family
    self.addr = addr
    self.mask_bits = mask_bits
  def __repr__(self):
    return f"{socket.inet_ntop(self.family, self.addr)}/{self.mask_bits}"
  def addr_str(self):
    return socket.inet_ntop(self.family, self.addr)

def random_ip4_subnet():
  """Generate a random IPv4 subnet (10.R.R.0/24)
  """
  while True:
    addr = b"\x0a" + os.urandom(2) + b"\0"
    if addr not in v4prefixes:
      v4prefixes.add(addr)
      break
  return InetAddr(socket.AF_INET, addr, 24)

def random_ip6_subnet():
  """Generate a random IPv6 subnet (fdRR:RRRR:RRRR:RRRR::0/64)
  """
  while True:
    addr6 = b"\xfd" + os.urandom(7) + 8 * b"\0"
    if addr6 not in v6prefixes:
      v6prefixes.add(addr6)
      break;
  return InetAddr(socket.AF_INET6, addr6, 64)

def host_addr(subnet, host_id):
  # Make sure `host_id` fits into host bits of address
  host_bits = len(subnet.addr)*8-subnet.mask_bits
  assert int.bit_length(host_id)<=host_bits, "Host identifier too large"
  # Combine `host_id` with subnet address
  i = subnet.mask_bits // 8
  result = bytearray(subnet.addr)
  for byte in int.to_bytes(host_id, length=(host_bits+7)//8, byteorder="big"):
    result[i] |= byte
    i += 1
  return InetAddr(subnet.family, bytes(result), subnet.mask_bits)

class ProcessWrapper:
  def __init__(self, args):
    kwargs = {}
    if not DEBUG:
      kwargs["stderr"] = subprocess.PIPE
      kwargs["stdout"] = subprocess.PIPE
    self._process = subprocess.Popen(args, **kwargs)

    def kill_process():
      os.kill(self._process.pid, signal.SIGINT)
      self._process.wait()
    atexit.register(kill_process)

class VirtualNetwork:
  def __init__(self, tempdir, netbootdir):
    self._tempdir = tempdir
    # Create and bring and bring upbridge
    self._br = next_bridge_name()
    runcmd(["ip", "link", "add", self._br, "type", "bridge"])
    runcmd(["ip", "link", "set", self._br, "up"])
    atexit.register(lambda: runcmd(["ip", "link", "del", self._br], assert_ok=False))
    # Create subnets
    self._v4subnet = random_ip4_subnet()
    self._v6subnet = random_ip6_subnet()
    # Create address for the host
    self._v4addr = host_addr(self._v4subnet, 1)
    self._v6addr = host_addr(self._v6subnet, 1)
    runcmd(["ip", "addr", "add", f"{self._v4addr}", "dev", self._br])
    runcmd(["ip", "addr", "add", f"{self._v6addr}", "dev", self._br])
    # Configure dnsmasq
    self._dnsmasq_conf = os.path.join(self._tempdir, "dnsmasq.conf")
    self._netbootdir = netbootdir
    with open(self._dnsmasq_conf, "w") as f:
      f.write(f"""\
# Disable DNS
port=0

# Bind DHCP and TFTP to bridge
interface={self._br}
bind-interfaces

# IPv4 client address range
dhcp-range={host_addr(self._v4subnet, 100).addr_str()},{host_addr(self._v4subnet, 200).addr_str()},12h

# IPv4 TFTP boot
dhcp-match=set:bios-tftp-x86,option:client-arch,0
dhcp-boot=tag:bios-tftp-x86,pxelinux.0
dhcp-match=set:efi-tftp-x86_64,option:client-arch,7
dhcp-boot=tag:efi-tftp-x86_64,bootx64.efi
dhcp-match=set:efi-tftp-arm64,option:client-arch,11
dhcp-boot=tag:efi-tftp-arm64,bootaa64.efi

# IPv4 HTTP boot
dhcp-match=set:efi-http-x86_64,option:client-arch,16
dhcp-option=tag:efi-http-x86_64,option:bootfile-name,http://{self._v4addr.addr_str()}:8000/bootx64.efi
dhcp-option=tag:efi-http-x86_64,option:vendor-class,HTTPClient
dhcp-match=set:efi-http-arm64,option:client-arch,19
dhcp-option=tag:efi-http-arm64,option:bootfile-name,http://{self._v4addr.addr_str()}:8000/bootaa64.efi
dhcp-option=tag:efi-http-arm64,option:vendor-class,HTTPClient

# IPv6 client address range
dhcp-range={host_addr(self._v6subnet, 0x1000).addr_str()},{host_addr(self._v6subnet, 0x2000).addr_str()},12h

# IPv6 TFTP boot
dhcp-match=set:efi-tftp-x86_64-v6,option6:61,7
dhcp-option=tag:efi-tftp-x86_64-v6,option6:bootfile-url,tftp://[{self._v6addr.addr_str()}]/bootx64.efi
dhcp-match=set:efi-tftp-arm64-v6,option6:61,11
dhcp-option=tag:efi-tftp-arm64-v6,option6:bootfile-url,tftp://[{self._v6addr.addr_str()}]/bootaa64.efi

# IPv6 HTTP boot
dhcp-match=set:efi-http-x86_64-v6,option6:61,16
dhcp-option=tag:efi-http-x86_64-v6,option6:bootfile-url,http://[{self._v6addr.addr_str()}]:8000/bootx64.efi
dhcp-option=tag:efi-http-x86_64-v6,option6:16,AA,HTTPClient
dhcp-match=set:efi-http-arm64-v6,option6:61,19
dhcp-option=tag:efi-http-arm64-v6,option6:bootfile-url,http://[{self._v6addr.addr_str()}]:8000/bootaa64.efi
dhcp-option=tag:efi-http-arm64-v6,option6:16,AA,HTTPClient

# FIXME: The line described below is a kludge to work around the seeming limitations of dnsmasq
# FIXME: The option we need to encode is 2 byte ID, 2 byte length, 4 byte enterprise ID, 2 byte length, string
# FIXME: The encoding dnsmasq does is 2 byte ID, 2 byte length, then for each value 2 byte length, string
# FIXME: Take length (2) + "AA" becomes the enterprise ID (which firmware ignores), so it all
# FIXME: works despite it being all horrible. Maybe we should use a different DHCP server.
# FIXME: dhcp-option=tag:efi-http-arm64-v6,option6:16,AA,HTTPClient

# TFTP server
enable-tftp
tftp-root={self._netbootdir}
""")
    # Start dnsmasq
    self._dnsmasq = ProcessWrapper(["dnsmasq",
      "--no-daemon", f"--conf-file={self._dnsmasq_conf}"])
    # Start HTTP servers
    import time
    time.sleep(5) # FIXME: wait for address assignment in a nicer way
    self._http_daemon_v4 = ProcessWrapper(["python3",
      "-m", "http.server",
      "-b", self._v4addr.addr_str(),
      "-d", self._netbootdir,
      "8000"])
    self._http_daemon_v6 = ProcessWrapper(["python3",
      "-m", "http.server",
      "-b", f"{self._v6addr.addr_str()}",
      "-d", self._netbootdir,
      "8000"])

  def new_tap(self):
    tap = next_tap_name()
    runcmd(["ip", "tuntap", "add", "mode", "tap", "dev", tap])
    runcmd(["ip", "link", "set", "dev", tap, "master", self._br])
    runcmd(["ip", "link", "set", "dev", tap, "up"])
    atexit.register(lambda: runcmd(["ip", "link", "del", tap], assert_ok=False))
    return tap
