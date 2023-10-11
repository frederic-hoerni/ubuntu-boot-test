# ubuntu-boot-test

This package contains a testing framework for the Ubuntu bootloader stack.
The goal of this effort is to provide assurance that changes to shim and GRUB
did not break booting Ubuntu on set of supported targets.

## Goals

* Continous testing of shim and GRUB updates via autopkgtest
* Manual testing of locally built copies of shim and GRUB
