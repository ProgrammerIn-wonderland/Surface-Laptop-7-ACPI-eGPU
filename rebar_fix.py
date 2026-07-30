#!/usr/bin/env python3
"""
rebar_fix.py -- find PCI devices failing with Code 12 and shrink their BAR
requests so they fit in the address space the platform actually offers.

Windows' PCI driver reads a card's Resizable BAR capability and asks the
arbiter for the largest advertised size (often 8-16 GiB) as the PREFERRED
option, with the native size (usually 256 MiB) only as an ALTERNATIVE.  On
platforms whose host-bridge windows are smaller than the ReBAR request, the
allocation fails and the device gets CM_PROB_NORMAL_CONFLICT -- Code 12.

This writes an OverrideConfigVector into the device's LogConf key containing
the same requirements with the oversized options removed.  Per Microsoft's
PnP documentation, when an override configuration is present the PnP manager
ignores the device's own resource requirements entirely, so the oversized
request never reaches the arbiter.

Read-only by default.  Nothing is written without --apply.

    python rebar_fix.py                    # list Code 12 devices, show plan
    python rebar_fix.py --reg out.reg      # emit .reg files, change nothing
    python rebar_fix.py --apply            # write to registry (needs admin)
    python rebar_fix.py --revert           # remove overrides this tool wrote

    --instance "PCI\\VEN_10DE&..."          # target one device
    --max-bar 3G                           # keep largest option <= 3 GiB
                                           # (default: smallest option)
"""

import argparse
import struct
import subprocess
import sys

REG_RESOURCE_REQUIREMENTS_LIST = 10

CM_MEMORY, CM_MEMORY_LARGE, CM_DEVICE_PRIVATE = 3, 7, 129
OPT_REQUIRED, OPT_PREFERRED, OPT_ALTERNATIVE = 0x00, 0x01, 0x08

FLAG_PREFETCHABLE = 0x0004
FLAG_LARGE_40, FLAG_LARGE_48, FLAG_LARGE_64 = 0x0200, 0x0400, 0x0800

DESC_SIZE = 32
HDR_SIZE = 32
LIST_HDR_SIZE = 8


def human(n):
    for unit, div in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if n >= div:
            return f"{n/div:,.0f} {unit}"
    return f"{n} B"


def parse_size(s):
    s = s.strip().upper()
    mult = 1
    if s.endswith("G"):
        mult, s = 2**30, s[:-1]
    elif s.endswith("M"):
        mult, s = 2**20, s[:-1]
    elif s.endswith("K"):
        mult, s = 2**10, s[:-1]
    return int(float(s) * mult)


class Descriptor:
    """One IO_RESOURCE_DESCRIPTOR (32 bytes)."""

    def __init__(self, raw):
        self.raw = bytearray(raw)
        (self.option, self.type, self.share, _spare1,
         self.flags, _spare2) = struct.unpack_from("<BBBBHH", raw, 0)
        self.length, self.alignment = struct.unpack_from("<II", raw, 8)
        self.minimum, self.maximum = struct.unpack_from("<QQ", raw, 16)

    @property
    def is_memory(self):
        return self.type in (CM_MEMORY, CM_MEMORY_LARGE)

    @property
    def actual_length(self):
        """MemoryLarge encodes size with a shift indicated by Flags."""
        if self.flags & FLAG_LARGE_40:
            return self.length << 8
        if self.flags & FLAG_LARGE_48:
            return self.length << 16
        if self.flags & FLAG_LARGE_64:
            return self.length << 32
        return self.length

    def set_option(self, opt):
        self.option = opt
        self.raw[0] = opt

    def describe(self):
        if self.type == CM_DEVICE_PRIVATE:
            return f"(tag: BAR{self.alignment})"
        if not self.is_memory:
            names = {1: "Port", 2: "Interrupt", 4: "DMA", 6: "BusNumber"}
            return names.get(self.type, f"type{self.type}")
        opt = {OPT_PREFERRED: "PREFERRED", OPT_ALTERNATIVE: "alternative",
               OPT_REQUIRED: "required"}.get(self.option, f"opt{self.option}")
        pf = "prefetchable" if self.flags & FLAG_PREFETCHABLE else "non-prefetchable"
        limit = " [must be <4GB]" if self.maximum <= 0xFFFFFFFF else ""
        return f"{opt:11} {human(self.actual_length):>9}  {pf:16}{limit}"


class ConfigVector:
    """An IO_RESOURCE_REQUIREMENTS_LIST: header + one or more alternative lists."""

    def __init__(self, data):
        self.data = bytes(data)
        if len(data) < HDR_SIZE:
            raise ValueError(f"too short to be a config vector ({len(data)} bytes)")
        self.header = bytearray(data[:HDR_SIZE])
        self.alt_count = struct.unpack_from("<I", data, 28)[0]
        if not 0 < self.alt_count < 64:
            raise ValueError(f"implausible AlternativeLists count: {self.alt_count}")

        self.lists = []
        off = HDR_SIZE
        for _ in range(self.alt_count):
            ver, rev, count = struct.unpack_from("<HHI", data, off)
            off += LIST_HDR_SIZE
            if count > 4096 or off + count * DESC_SIZE > len(data):
                raise ValueError(f"descriptor count {count} overruns the buffer")
            descs = [Descriptor(data[off + i * DESC_SIZE: off + (i + 1) * DESC_SIZE])
                     for i in range(count)]
            off += count * DESC_SIZE
            self.lists.append({"version": ver, "revision": rev, "descriptors": descs})

    def group_by_bar(self, descriptors):
        """pci.sys emits resource options followed by a DevicePrivate tag whose
        Alignment field carries the BAR index.  Returns [(bar|None, [descs])]."""
        groups, pending = [], []
        for d in descriptors:
            if d.type == CM_DEVICE_PRIVATE:
                groups.append((d.alignment, pending, d))
                pending = []
            else:
                pending.append(d)
        if pending:
            groups.append((None, pending, None))
        return groups

    def shrink(self, max_bar=None):
        """Keep one memory option per BAR: the largest that fits under max_bar,
        or the smallest offered if max_bar is None.  Returns a change log."""
        changes = []
        for li, lst in enumerate(self.lists):
            kept = []
            for bar, options, tag in self.group_by_bar(lst["descriptors"]):
                mem = [d for d in options if d.is_memory]
                other = [d for d in options if not d.is_memory]

                if len(mem) <= 1:
                    kept.extend(other + mem)
                    if tag:
                        kept.append(tag)
                    continue

                if max_bar is None:
                    choice = min(mem, key=lambda d: d.actual_length)
                else:
                    fitting = [d for d in mem if d.actual_length <= max_bar]
                    if not fitting:
                        changes.append((li, bar, None, None,
                                        "no option fits the cap; left unchanged"))
                        kept.extend(other + mem)
                        if tag:
                            kept.append(tag)
                        continue
                    choice = max(fitting, key=lambda d: d.actual_length)

                dropped = [d for d in mem if d is not choice]
                if choice.option != OPT_PREFERRED:
                    choice.set_option(OPT_PREFERRED)
                changes.append((li, bar, choice, dropped, None))
                kept.extend(other + [choice])
                if tag:
                    kept.append(tag)
            lst["descriptors"] = kept
        return changes

    def serialize(self):
        out = bytearray(self.header)
        struct.pack_into("<I", out, 28, len(self.lists))
        for lst in self.lists:
            out += struct.pack("<HHI", lst["version"], lst["revision"],
                               len(lst["descriptors"]))
            for d in lst["descriptors"]:
                out += d.raw
        struct.pack_into("<I", out, 0, len(out))
        return bytes(out)

    def dump(self, indent="    "):
        for li, lst in enumerate(self.lists):
            if len(self.lists) > 1:
                print(f"{indent}-- alternative list {li}")
            for bar, options, _tag in self.group_by_bar(lst["descriptors"]):
                label = f"BAR{bar}" if bar is not None else "untagged"
                for d in options:
                    print(f"{indent}{label:9} {d.describe()}")


# ---------------------------------------------------------------- Windows glue

def find_problem_devices(code=12):
    """Win32_PnPEntity.ConfigManagerErrorCode == 12 -> CM_PROB_NORMAL_CONFLICT."""
    ps = (f"Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode={code}' "
          f"| Where-Object {{ $_.PNPDeviceID -like 'PCI\\*' }} "
          f"| ForEach-Object {{ $_.PNPDeviceID + '|' + $_.Name }}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=90)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"could not query devices: {e}", file=sys.stderr)
        return []
    devices = []
    for line in out.stdout.splitlines():
        if "|" in line:
            inst, name = line.strip().split("|", 1)
            devices.append((inst, name))
    return devices


def logconf_path(instance):
    return r"SYSTEM\CurrentControlSet\Enum" + "\\" + instance + r"\LogConf"


def read_vector(instance, value="BasicConfigVector"):
    import winreg
    path = logconf_path(instance)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
        data, _typ = winreg.QueryValueEx(k, value)
    return bytes(data)


def write_vector(instance, data):
    import winreg
    path = logconf_path(instance)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "OverrideConfigVector", 0,
                          REG_RESOURCE_REQUIREMENTS_LIST, data)


def delete_override(instance):
    import winreg
    path = logconf_path(instance)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.DeleteValue(k, "OverrideConfigVector")


def emit_reg(instance, data, path, undo_path=None):
    key = r"HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Enum" + "\\" + instance + r"\LogConf"
    hexs = [f"{b:02x}" for b in data]
    lines, cur = [], '"OverrideConfigVector"=hex(a):'
    for i, h in enumerate(hexs):
        piece = h + ("," if i < len(hexs) - 1 else "")
        if len(cur) + len(piece) > 76:
            lines.append(cur + "\\")
            cur = "  "
        cur += piece
    lines.append(cur)
    with open(path, "w", encoding="utf-8") as f:
        f.write("Windows Registry Editor Version 5.00\n\n[%s]\n%s\n"
                % (key, "\n".join(lines)))
    if undo_path:
        with open(undo_path, "w", encoding="utf-8") as f:
            f.write('Windows Registry Editor Version 5.00\n\n[%s]\n'
                    '"OverrideConfigVector"=-\n' % key)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", help="target one device instance path")
    ap.add_argument("--max-bar", help="keep largest option <= this (e.g. 3G, 256M)")
    ap.add_argument("--apply", action="store_true", help="write to the registry")
    ap.add_argument("--revert", action="store_true", help="remove the override")
    ap.add_argument("--reg", metavar="FILE", help="emit .reg files instead")
    ap.add_argument("--from-file", metavar="BIN",
                    help="parse a raw vector from disk (offline testing)")
    args = ap.parse_args()

    cap = parse_size(args.max_bar) if args.max_bar else None

    if args.from_file:
        vec = ConfigVector(open(args.from_file, "rb").read())
        print("current requirements:")
        vec.dump()
        for _li, bar, choice, dropped, note in vec.shrink(cap):
            if note:
                print(f"  BAR{bar}: {note}")
            else:
                for d in dropped:
                    print(f"  BAR{bar}: dropping {human(d.actual_length)}")
                print(f"  BAR{bar}: keeping  {human(choice.actual_length)} as PREFERRED")
        print("\nresulting requirements:")
        vec.dump()
        print(f"\n{len(vec.data)} bytes -> {len(vec.serialize())} bytes")
        return 0

    if sys.platform != "win32":
        print("this needs Windows (use --from-file to test parsing)", file=sys.stderr)
        return 2

    if args.instance:
        targets = [(args.instance, "(specified)")]
    else:
        targets = find_problem_devices()
        if not targets:
            print("no PCI devices are reporting Code 12.")
            return 0

    for instance, name in targets:
        print("=" * 72)
        print(f"{name}\n  {instance}")

        if args.revert:
            try:
                delete_override(instance)
                print("  removed OverrideConfigVector")
            except FileNotFoundError:
                print("  no OverrideConfigVector present")
            except PermissionError:
                print("  access denied -- run elevated")
            continue

        try:
            raw = read_vector(instance)
        except FileNotFoundError:
            print("  no BasicConfigVector under LogConf; skipping")
            continue
        except PermissionError:
            print("  access denied reading LogConf -- run elevated")
            continue

        try:
            vec = ConfigVector(raw)
        except ValueError as e:
            print(f"  could not parse the vector: {e}")
            continue

        print("\n  current requirements:")
        vec.dump("    ")

        changes = vec.shrink(cap)
        if not any(c[2] for c in changes):
            print("\n  no BAR offers more than one size; nothing to shrink.")
            print("  Code 12 here is not a ReBAR problem.")
            continue

        print()
        for _li, bar, choice, dropped, note in changes:
            if note:
                print(f"  BAR{bar}: {note}")
            else:
                for d in dropped:
                    print(f"  BAR{bar}: drop {human(d.actual_length)}")
                print(f"  BAR{bar}: keep {human(choice.actual_length)} as PREFERRED")

        new = vec.serialize()
        print(f"\n  vector: {len(raw)} -> {len(new)} bytes")

        if args.reg:
            undo = args.reg.replace(".reg", "-UNDO.reg")
            emit_reg(instance, new, args.reg, undo)
            print(f"  wrote {args.reg} and {undo}")
        elif args.apply:
            try:
                write_vector(instance, new)
                print("  wrote OverrideConfigVector")
                print("  now replug the device, or disable/enable it in Device Manager")
            except PermissionError:
                print("  access denied -- run elevated")
        else:
            print("  (dry run -- pass --apply to write, or --reg FILE to export)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
