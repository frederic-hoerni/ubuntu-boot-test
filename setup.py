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
        "ubuntu_boot_test_run",
    ),
)
