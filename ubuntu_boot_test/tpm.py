#
# ubuntu-boot-test: tpm.py: swtpm wrapper
#
# Copyright (C) 2025 Canonical, Ltd.
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
import os

class TPM:
  def __init__(self, tempdir):
    self._tempdir = tempdir
    self._pidfile = os.path.join(tempdir, "swtpm.pid")
    self._statedir = os.path.join(tempdir, "swtpm.state")
    os.makedirs(self._statedir, exist_ok=True)
    self._sock = os.path.join(tempdir, "swtpm.socket")
    self._ctrlsock = os.path.join(tempdir, "swtpm.socket.ctrl")
    self.run_process()

  def run_process(self):
    if hasattr(self, "_process"):
      # Kill previous process if present
      self._process.kill_process()
      # Wait for it to go away
      while os.access(self._pidfile, os.O_RDONLY):
        pass

    # Create process
    self._process = ProcessWrapper(["swtpm",
      "socket", "--tpm2",
      "--pid", f"file={self._pidfile}",
      "--tpmstate", f"dir={self._statedir}",
      "--server", f"type=unixio,path={self._sock},mode=0666",
      "--ctrl", f"type=unixio,path={self._ctrlsock},mode=0666",
      "--flags", "not-need-init,startup-clear",
    ])
    # Wait for it to start
    while not os.access(self._pidfile, os.O_RDONLY):
      pass
    # Send startup command
    # NOTE: This shouldnt be needed and should be done by the BIOS
    # but something is in a bad state without it.
    self.startup()

  def sock(self):
    return self._sock

  def ctrlsock(self):
    return self._ctrlsock

  def generate_srk(self, outdir):
    runcmd(["tpm2_createprimary",
      "-T", f"swtpm:path={self._sock}",
      "-c", os.path.join(outdir, "srk.ctx"),
    ])
    runcmd(["tpm2_readpublic",
      "-T", f"swtpm:path={self._sock}",
      "-c", os.path.join(outdir, "srk.ctx"),
      "-o", os.path.join(outdir, "srk.pub"),
    ])

  def startup(self):
    runcmd(["tpm2_startup", "-T", f"swtpm:path={self._sock}", "-c"])
