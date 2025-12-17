import typing as tp
from dataclasses import dataclass

from ubuntu_boot_test.vm import VirtualMachine


@dataclass
class EFIVars:
    """
    Pythonic representation of EFI Boot Variables.
    Maintains the BootOrder, BootCurrent, BootNext, and BootXXXX entries.
    """

    boot_order: tp.List[int]
    boot_current: int
    entries: tp.Dict[int, str]
    boot_next: tp.Optional[int] = None

    def __post_init__(self):
        """
        Ensures everything is set after generation except BootNext, which
        can be None.
        """
        if not (self.boot_current and self.boot_order and self.entries):
            var_dict = self.__dict__.copy()
            var_dict["msg"] = f"Invalid system detected: only 'boot_next' may be empty"

            raise ValueError(var_dict)

    @classmethod
    def from_efibootmgr(cls, vm: VirtualMachine) -> "EFIVars":
        """Reports the EFI boot variables from the managed VM."""
        raw_vars = vm.run_cmd(["efibootmgr"])
        var_lines = raw_vars.splitlines()
        raw_bootvars = [var[4:] for var in var_lines if var.startswith("Boot")]

        boot_order = []
        boot_current = boot_next = None
        entries = {}
        for bootvar in raw_bootvars:
            if bootvar.startswith("Current"):
                # BootCurrent: XXXX
                boot_current = int(bootvar.split()[1], 16)
            elif bootvar.startswith("Next"):
                # BootNext: XXXX
                boot_next = int(bootvar.split()[1], 16)
            elif bootvar.startswith("Order"):
                # BootOrder: 0001,0002,0003,...,XXXX
                raw_order = bootvar.split()[1]
                order_str = raw_order.split(",")
                boot_order = [int(num_str, 16) for num_str in order_str]
            else:
                # Only gathering number and content for now since there are
                # spaces in each column after the boot number
                # "BootXXXX <content>" OR "BootXXXX* <content>"
                boot_num = int(bootvar[:4], 16)
                content = bootvar[bootvar.index(" ") :].strip()
                entries[boot_num] = content

        return cls(
            boot_current=boot_current,
            boot_next=boot_next,
            boot_order=boot_order,
            entries=entries,
        )

    def dump(self) -> tp.Dict[str, str]:
        """
        Output data in a more ``efibootmgr``-like style.
        Useful for error messages and debugging.
        """
        dump_dict = {}
        if self.boot_next:
            dump_dict["BootNext"] = boot_entry_hex_str(self.boot_next)
        dump_dict["BootCurrent"] = boot_entry_hex_str(self.boot_current)
        dump_dict["BootOrder"] = boot_order_hex_str(self.boot_order)
        for boot_num, description in self.entries.items():
            dump_dict[f"Boot{boot_entry_hex_str(boot_num)}"] = description

        return dump_dict
