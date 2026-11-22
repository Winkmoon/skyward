#!/usr/bin/env python3
"""Build a Skyward recovery image on top of a legacy (boot header v2) recovery
partition image, as the OPPO A9x (PCEM00, MT6761/MT6771, A-only, no super)
needs.

This mirrors what crates/skyward-pack does for GKI (header v3/v4, lz4) images,
but works with the old Android 9 style Android boot image the phone's
bootloader reads:

  * parses an Android boot image header v2 (kernel / ramdisk / second /
    recovery_dtbo / dtb sections), keeping the kernel and device tree as they
    are;
  * decompresses the gzip'd ramdisk cpio, edits it in place (drops the TWRP
    leftovers, installs the Skyward binary, its configuration, fonts and the
    commands it answers), and compresses it again;
  * rebuilds the v2 image with the original addresses, page size and command
    line;
  * appends the unsigned AVB hash footer, byte for byte as
    crates/skyward-pack/src/avb.rs lays it out, and fills the partition.

Usage:
  python3 tools/pack_recovery.py \
      --stock stock/pcem00/recovery.img \
      --binary target/aarch64-linux-android/release/skyward \
      --system-init target/system-init/aarch64-linux-android/release/system-init \
      --config devices/pcem00.toml \
      --font resources/fonts/MiSansVF.ttf \
      --mono resources/fonts/KirkMono-Regular.ttf \
      --mono-license resources/fonts/KirkMono-OFL.txt \
      --out out/skyward-pcem00.img
"""

import argparse
import gzip
import hashlib
import io
import os
import struct
import sys

MAGIC = b"ANDROID!"
CPIO_MAGIC = b"070701"
S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFREG = 0o100000
S_IFLNK = 0o120000

# The ramdisk paths Skyward is installed at; the same constants as
# skyward_config::ramdisk.
RECOVERY = "system/bin/recovery"
CONFIG = "skyward/device.toml"
FONT = "skyward/MiSansVF.ttf"
MONO = "skyward/KirkMono-Regular.ttf"
MONO_LICENSE = "skyward/KirkMono-OFL.txt"
SYSTEM_INIT = "skyward/system-init"
COMMANDS = ["system/bin/7za", "system/bin/avbctl", "system/bin/resetprop",
            "system/bin/unzip", "system/bin/zstd"]
PROP_DEFAULT = "prop.default"
TASK_PROFILES = "system/etc/task_profiles.json"
MTP_RC = "system/etc/init/skyward-mtp.rc"
BUILD_VERSION = ["ro.build.version.release", "ro.build.version.security_patch",
                 "ro.vendor.build.security_patch"]
# TWRP leftovers: the third-party tree, its bolted-on FBE and WiFi scripts,
# which Skyward replaces with its own HAL orchestration and WLAN bring-up,
# and the shell utilities Skyward does not run.
DROP_NAMES = {
    "twres", "twrp_ramdisk-timestamp", "relink_binaries-timestamp",
    "relink_libraries-timestamp", "relink_vendor_hw_binaries-timestamp",
    "system/etc/twrp.flags", "system/etc/twrp-wifi",
    "system/bin/twrp-wifi.sh", "system/bin/twrp-fbedec-prep.sh",
    "system/bin/twrp-fbedec-ui.sh", "system/bin/twrp_fbedec",
    "system/bin/twrp_fbedec2", "system/bin/busybox", "system/bin/bash",
    "system/bin/nano",
}

# The room LK reserves for the ramdisk at 0x55000000 (ramdisk_addr_mb): a
# larger ramdisk runs into LK's own memory at 0x57000000, and LK resets the
# phone instead of starting the kernel.
LK_RAMDISK_ROOM = 0x2000000
EMPTY_FILES = {
    "init.recovery.mt6771.rc": "# The TWRP FBE wrapper lived here; Skyward starts\n# the phone's own key programs from its init scripts.\n",
    "init.recovery.trustonic.rc": "# The TWRP Trustonic setup lived here.\n",
}


def die(msg):
    sys.stderr.write("pack_recovery: " + msg + "\n")
    sys.exit(1)


def align(n, unit):
    return (n + unit - 1) // unit * unit


# ---------------------------------------------------------------------------
# Android boot image, header version 2
# ---------------------------------------------------------------------------

def parse_boot_image(data):
    if data[:8] != MAGIC:
        die("not an Android boot image (magic %r)" % data[:8])
    (kernel_size, kernel_addr, ramdisk_size, ramdisk_addr, second_size,
     second_addr, tags_addr, page_size, header_version, os_version) = \
        struct.unpack_from("<10I", data, 8)
    if header_version != 2:
        die("boot header version %d, this packer handles version 2"
            % header_version)
    header_size, = struct.unpack_from("<I", data, 1644)
    recovery_dtbo_size, = struct.unpack_from("<I", data, 1632)
    dtb_size, dtb_addr = struct.unpack_from("<IQ", data, 1648)
    if header_size > page_size:
        die("header is %d bytes, page size is %d" % (header_size, page_size))

    off = align(page_size, page_size)
    kernel = data[off:off + kernel_size]
    off = align(off + kernel_size, page_size)
    ramdisk = data[off:off + ramdisk_size]
    off = align(off + ramdisk_size, page_size)
    second = data[off:off + second_size]
    off = align(off + second_size, page_size)
    recovery_dtbo = data[off:off + recovery_dtbo_size]
    off = align(off + recovery_dtbo_size, page_size)
    dtb = data[off:off + dtb_size]
    off = align(off + dtb_size, page_size)
    for name, part, size in (("kernel", kernel, kernel_size),
                             ("ramdisk", ramdisk, ramdisk_size),
                             ("second", second, second_size),
                             ("recovery_dtbo", recovery_dtbo, recovery_dtbo_size),
                             ("dtb", dtb, dtb_size)):
        if len(part) != size:
            die("%s runs past the end of the image" % name)
    return {
        "header": data[:header_size],
        "page_size": page_size,
        "kernel": kernel,
        "ramdisk": ramdisk,
        "second": second,
        "recovery_dtbo": recovery_dtbo,
        "dtb": dtb,
    }


def build_boot_image(d, ramdisk):
    hdr = bytearray(d["header"])
    struct.pack_into("<I", hdr, 8, len(d["kernel"]))
    struct.pack_into("<I", hdr, 16, len(ramdisk))
    struct.pack_into("<I", hdr, 24, len(d["second"]))
    struct.pack_into("<I", hdr, 1632, len(d["recovery_dtbo"]))
    struct.pack_into("<I", hdr, 1648, len(d["dtb"]))
    # The id is the kernel's and ramdisk's own SHA-1, as the AOSP mkbootimg
    # names it. Unlocked bootloaders do not check it. The field is thirty-two
    # bytes and a SHA-1 is twenty, so the rest stays zero: writing twenty
    # bytes over it would shorten the header.
    sha = hashlib.sha1()
    for part in (d["kernel"], ramdisk, d["second"]):
        sha.update(part)
        sha.update(struct.pack("<I", len(part)))
    hdr[576:596] = sha.digest()
    page = d["page_size"]
    out = bytearray()
    out += hdr
    out += b"\0" * (align(len(out), page) - len(out))
    for part in (d["kernel"], ramdisk, d["second"], d["recovery_dtbo"], d["dtb"]):
        out += part
        out += b"\0" * (align(len(out), page) - len(out))
    return bytes(out)


# ---------------------------------------------------------------------------
# newc cpio
# ---------------------------------------------------------------------------

class Entry:
    __slots__ = ("ino", "mode", "uid", "gid", "nlink", "mtime", "devmaj",
                 "devmin", "rdevmaj", "rdevmin", "name", "data")

    def __init__(self, ino, mode, uid, gid, nlink, mtime, devmaj, devmin,
                 rdevmaj, rdevmin, name, data):
        self.ino = ino
        self.mode = mode
        self.uid = uid
        self.gid = gid
        self.nlink = nlink
        self.mtime = mtime
        self.devmaj = devmaj
        self.devmin = devmin
        self.rdevmaj = rdevmaj
        self.rdevmin = rdevmin
        self.name = name
        self.data = data

    def is_dir(self):
        return self.mode & S_IFMT == S_IFDIR

    def is_link(self):
        return self.mode & S_IFMT == S_IFLNK


def parse_cpio(buf):
    entries = []
    i = 0
    while True:
        if buf[i:i + 6] != CPIO_MAGIC:
            die("bad cpio magic at %d: %r" % (i, buf[i:i + 6]))
        fields = [int(buf[i + 6 + 8 * n:i + 14 + 8 * n], 16) for n in range(13)]
        (ino, mode, uid, gid, nlink, mtime, filesize, devmaj, devmin,
         rdevmaj, rdevmin, namesize, _check) = fields
        name_at = i + 110
        name = buf[name_at:name_at + namesize - 1].decode("utf-8", "surrogateescape")
        data_at = align(name_at + namesize, 4)
        data = buf[data_at:data_at + filesize]
        i = align(data_at + filesize, 4)
        if name == "TRAILER!!!":
            break
        entries.append(Entry(ino, mode, uid, gid, nlink, mtime, devmaj, devmin,
                             rdevmaj, rdevmin, name, data))
    return entries


def write_cpio(entries):
    out = bytearray()

    def put(e):
        name = e.name.encode("utf-8", "surrogateescape") + b"\0"
        fields = [e.ino, e.mode, e.uid, e.gid, e.nlink, e.mtime, len(e.data),
                  e.devmaj, e.devmin, e.rdevmaj, e.rdevmin, len(name), 0]
        hdr = CPIO_MAGIC + b"".join(b"%08X" % f for f in fields)
        out.extend(hdr)
        out.extend(name)
        out.extend(b"\0" * (align(len(out), 4) - len(out)))
        out.extend(e.data)
        out.extend(b"\0" * (align(len(out), 4) - len(out)))

    for e in entries:
        put(e)
    trailer = Entry(0, 0, 0, 0, 1, 0, 0, 0, 0, 0, "TRAILER!!!", b"")
    put(trailer)
    # The kernel wants the archive to end on a 512-byte boundary.
    out.extend(b"\0" * (align(len(out), 512) - len(out)))
    return bytes(out)


# ---------------------------------------------------------------------------
# The ramdisk Skyward runs from
# ---------------------------------------------------------------------------

def find(entries, name):
    for e in entries:
        if e.name == name:
            return e
    return None


def replace_or_add(entries, name, mode, data):
    e = find(entries, name)
    if e is not None:
        if (e.mode & S_IFMT) != S_IFREG:
            die("%s is in the ramdisk and is not a regular file" % name)
        e.data = data
        e.mode = S_IFREG | mode
        return
    add_file(entries, name, mode, data)


def add_dirs(entries, name):
    parts = name.split("/")[:-1]
    for n in range(1, len(parts) + 1):
        parent = "/".join(parts[:n])
        if find(entries, parent) is None:
            e = Entry(0, S_IFDIR | 0o755, 0, 0, 2, 0, 0, 0, 0, 0, parent, b"")
            entries.append(e)


def add_file(entries, name, mode, data):
    if find(entries, name) is not None:
        die("%s is already in the ramdisk" % name)
    add_dirs(entries, name)
    entries.append(Entry(0, S_IFREG | mode, 0, 0, 1, 0, 0, 0, 0, 0, name, data))


def add_link(entries, name, target):
    e = find(entries, name)
    if e is not None:
        e.mode = S_IFLNK | 0o755
        e.data = target.encode()
        return
    add_dirs(entries, name)
    entries.append(Entry(0, S_IFLNK | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, name,
                         target.encode()))


def without_version_lines(props):
    kept = []
    for line in props.split(b"\n"):
        key = line.split(b"=")[0]
        if key.decode("utf-8", "replace") not in BUILD_VERSION:
            kept.append(line)
    return b"\n".join(kept)


def edit_ramdisk(ramdisk, args):
    try:
        cpio = gzip.decompress(ramdisk)
    except OSError as e:
        die("the stock ramdisk is not gzip: %s" % e)
    entries = parse_cpio(cpio)
    print("stock ramdisk: %d entries, %d bytes of cpio" % (len(entries), len(cpio)))

    # Drop the third-party recovery's own tree and bolted-on scripts: it is
    # the ramdisk's environment that is reused, not TWRP's code.
    kept = []
    in_dropped_dir = set()
    for e in entries:
        drop = e.name in DROP_NAMES
        for d in list(DROP_NAMES):
            if e.name.startswith(d + "/"):
                drop = True
        if drop:
            in_dropped_dir.add(e.name)
            continue
        if e.name in EMPTY_FILES:
            e.data = EMPTY_FILES[e.name].encode()
        kept.append(e)
    entries = kept
    print("dropped %d TWRP entries" % len(in_dropped_dir))

    if find(entries, RECOVERY) is None:
        die("%s is not in the stock ramdisk" % RECOVERY)
    if find(entries, PROP_DEFAULT) is None:
        die("%s is not in the stock ramdisk" % PROP_DEFAULT)

    replace_or_add(entries, RECOVERY, 0o755, args.binary)
    add_file(entries, SYSTEM_INIT, 0o755, args.system_init)
    add_file(entries, CONFIG, 0o644, args.config)
    add_file(entries, FONT, 0o644, args.font)
    add_file(entries, MONO, 0o644, args.mono)
    add_file(entries, MONO_LICENSE, 0o644, args.mono_license)
    for command in COMMANDS:
        add_link(entries, command, "recovery")
    if find(entries, TASK_PROFILES) is None:
        add_file(entries, TASK_PROFILES, 0o644, args.task_profiles)
    add_file(entries, MTP_RC, 0o644, args.mtp_rc)

    props = find(entries, PROP_DEFAULT)
    edited = without_version_lines(props.data)
    if not edited.endswith(b"\n"):
        edited += b"\n"
    edited += b"ro.secure=0\nro.adb.secure=0\nro.debuggable=1\nro.ota.allow_downgrade=true\n"
    props.data = edited

    archive = write_cpio(entries)
    compressed = gzip.compress(archive, 9, mtime=0)
    print("new ramdisk: %d entries, %d bytes of cpio, %d bytes gzip" %
          (len(entries), len(archive), len(compressed)))
    if len(compressed) > LK_RAMDISK_ROOM:
        die("the ramdisk is %d bytes gzip, past the %d bytes LK reserves for "
            "it at 0x55000000: it would run into LK's own memory at "
            "0x57000000 and LK would reset the phone instead of starting the "
            "kernel" % (len(compressed), LK_RAMDISK_ROOM))
    print("ramdisk room left: %d bytes" % (LK_RAMDISK_ROOM - len(compressed)))
    installed = [e.name for e in entries if e.name.startswith("skyward/")]
    print("skyward files: %s" % ", ".join(sorted(installed)))
    return compressed


# ---------------------------------------------------------------------------
# The unsigned AVB hash footer, as crates/skyward-pack/src/avb.rs lays it out
# ---------------------------------------------------------------------------

AVB_BLOCK = 4096
AVB_FOOTER_LEN = 64
AVB_HEADER_LEN = 256
AVB_TAG_HASH = 2
AVB_RELEASE = b"skyward-pack"


def avb_hash_descriptor(image_size, partition, salt, digest):
    algorithm = b"sha256" + b"\0" * 26
    d = bytearray()
    d += struct.pack(">Q", AVB_TAG_HASH)
    d += struct.pack(">Q", 0)          # patched below
    d += struct.pack(">Q", image_size)
    d += algorithm
    d += struct.pack(">IIII", len(partition), len(salt), len(digest), 0)
    d += b"\0" * 60
    d += partition.encode()
    d += salt
    d += digest
    d += b"\0" * (align(len(d), 8) - len(d))
    struct.pack_into(">Q", d, 8, len(d) - 16)
    return bytes(d)


def avb_vbmeta(descriptors):
    aux = align(len(descriptors), 64)
    v = bytearray()
    v += b"AVB0"
    v += struct.pack(">II", 1, 0)
    v += struct.pack(">QQ", 0, aux)
    v += struct.pack(">I", 0)          # no signing algorithm
    for field in (0, 0, 0, 0, len(descriptors), 0, len(descriptors), 0, 0,
                  len(descriptors), 0):
        v += struct.pack(">Q", field)
    v += struct.pack(">II", 0, 0)      # flags, rollback index location
    v += AVB_RELEASE
    v += b"\0" * (AVB_HEADER_LEN - len(v))
    v += descriptors
    v += b"\0" * (AVB_HEADER_LEN + aux - len(v))
    return bytes(v)


def avb_footed(image, partition, partition_size):
    salt = hashlib.sha256(image).digest()
    digest = hashlib.sha256(salt + image).digest()
    vbmeta = avb_vbmeta(avb_hash_descriptor(len(image), partition, salt, digest))
    offset = align(len(image), AVB_BLOCK)
    end = offset + align(len(vbmeta), AVB_BLOCK)
    if end + AVB_BLOCK > partition_size:
        die("a %d-byte image and its vbmeta image (%d bytes) do not fit a "
            "%d-byte partition beside its footer" %
            (len(image), len(vbmeta), partition_size))
    out = bytearray()
    out += image
    out += b"\0" * (offset - len(out))
    out += vbmeta
    out += b"\0" * (partition_size - AVB_FOOTER_LEN - len(out))
    out += b"AVBf"
    out += struct.pack(">II", 1, 0)
    out += struct.pack(">QQQ", len(image), offset, len(vbmeta))
    out += b"\0" * (partition_size - len(out))
    return bytes(out)


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stock", required=True, help="whole recovery partition image")
    p.add_argument("--binary", required=True, help="skyward built for the phone")
    p.add_argument("--system-init", required=True)
    p.add_argument("--config", required=True, help="devices/<codename>.toml")
    p.add_argument("--font", required=True)
    p.add_argument("--mono", required=True)
    p.add_argument("--mono-license", required=True)
    p.add_argument("--mtp-rc", default="crates/skyward-pack/src/mtp.rc")
    p.add_argument("--task-profiles", default="crates/skyward-pack/src/task_profiles.json")
    p.add_argument("--partition-size", type=int, default=0,
                   help="bytes; defaults to the stock image's size")
    p.add_argument("--out", required=True)
    a = p.parse_args()

    def read(path):
        with open(path, "rb") as f:
            return f.read()

    a.binary = read(a.binary)
    a.system_init = read(a.system_init)
    a.config = read(a.config)
    a.font = read(a.font)
    a.mono = read(a.mono)
    a.mono_license = read(a.mono_license)
    a.mtp_rc = read(a.mtp_rc)
    a.task_profiles = read(a.task_profiles)

    stock = read(a.stock)
    partition_size = a.partition_size or len(stock)
    print("stock image: %d bytes, partition %d bytes" % (len(stock), partition_size))
    img = parse_boot_image(stock)
    print("boot header v2: page %d, kernel %d, ramdisk %d, dtb %d" %
          (img["page_size"], len(img["kernel"]), len(img["ramdisk"]), len(img["dtb"])))

    ramdisk = edit_ramdisk(img["ramdisk"], a)
    image = build_boot_image(img, ramdisk)
    print("boot image: %d bytes" % len(image))
    out = avb_footed(image, "recovery", partition_size)
    print("partition image: %d bytes, ends with an AVB footer" % len(out))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "wb") as f:
        f.write(out)
    print("wrote %s" % a.out)


if __name__ == "__main__":
    main()
