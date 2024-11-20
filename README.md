# ubuntu-boot-test

Automated QA for bootloader packages in Ubuntu.

## Production autopkgtest

Automated QA relies on the production [https://ubuntu-packaging-guide.readthedocs.io/en/latest/ubuntu-packaging-guide/auto-pkg-test.html](autopkgtest)
service maintained by the Ubuntu Release Management team.

1. [https://launchpad.net/ubuntu/+source/ubuntu-boot-test](ubuntu-boot-test) is packaged in the main Ubuntu archive.
2. Tested bootloader packages are marked as dependencies of the ubuntu-boot-test package.
3. When a new version of such a package is uploaded to a `<release>-proposed` pocket of the main Ubuntu archive,
   its migration to the `<release>` or `<release>-updates` (for stable releases) pocket (and hence availability to users)
   is gated against ubuntu-boot-test passing.

## PPA autopkgtest

Manual tests can be requested against bootloader packages uploaded to Launchpad PPAs.

This procedure can also be used for testing a new version of ubuntu-boot-test itself.

The requester needs to have the appropriate permission to request autopkgtests.

1. Upload the package to be tested to a PPA (e.g. `~randomdev/boot-test`).
2. Perform a HTTP GET request to the following URL (with the appropriate substitutions):
   `https://autopkgtest.ubuntu.com/request.cgi?release=<RELEASE>&arch=<ARCH>&package=ubuntu-boot-test&ppa=randomdev/boot-test&trigger=<boot-package>/<version>`
3. The test results will be available at:
   `https://autopkgtest.ubuntu.com/results/autopkgtest-<RELEASE>-randomdev-boot-test/`

## Local autopkgtest

For the regular development workflow, see `Using ubuntu-boot-test directly`.

This section is for testing the autopkgtest setup end-to-end without uploading to Launchpad.

1. Build the ubuntu-boot-test source package using `dpkg-buildpackage -S -d --no-sign`.
2. Create an autopkgtest virtual machine:
  `autopkgtest-buildvm-ubuntu-cloud -a <ARCH> -r <RELEASE>`
3. Run ubuntu-boot-test inside the autopkgtest virtual machine locally:
   `autopkgtest -U --timeout-factor 10 ./ubuntu-boot-test_<VERSION>.dsc -- qemu --timeout-reboot 1000 --show-boot --dpkg-architecture <ARCH> --ram-size 1536 autopkgtest-<RELEASE>-<ARCH>.img`
   This will use the local ubuntu-boot-test to test the latest packages inside the specified <RELEASE>.
   Local bootloader debs are (currently) difficult to test using this method.

## Using ubuntu-boot-test directly

This is the least _automatic_, but fastest (in terms of execution time) way to use ubuntu-boot-test.

Your test environment:
- Real machine, chroot, or container running Ubuntu.
- Kernel support for vsockets is present and `/dev/vhost-vsock` is accessible.
- For best performance, `/dev/kvm` is accessible and virtualization is available
  (this means nested virtualization in case of running ubuntu-boot-test itself in a VM).
- Netboot tests require running as root for setting up the network bridge and taps.

1. Install the dependencies listed in `debian/control` under `Depends`.
2. Select the desired test (see `ubuntu_boot_test/cmd_*.py`).
2. Collect the packages required for the desired test (see `PACKAGE_SETS` in the file from 2).
   Please note that you will need to provide a deb file for all members of the package set.
   Simply download the old ones for packages you did not modify, and provide the new ones for
   the rest.
3. Execute `./ubuntu_boot_test_run <TESTNAME> -r <RELEASE> -a <ARCH> package_dir/*.deb
