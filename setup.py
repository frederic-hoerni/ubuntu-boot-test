from setuptools import setup, find_packages

setup(
    name="ubuntu-boot-test",
    version="1",
    install_requires=[
        "pyOpenSSL",
        "pexpect",
        "pyuefivars",
        "requests",
    ],
    include_package_data=True,
    packages=find_packages(
        include=["ubuntu_boot_test"],
    ),
    scripts=(
        "bin/test_pcbios",
        "bin/test_uefi_noshim",
        "bin/test_uefi_shim"
    ),
)
