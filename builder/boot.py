"""Offline boot image and vbmeta transformations."""
import os
import re
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import libarchive

from filesystems import patch_fstab
from super_image import run


def unpack(image, folder):
    code = subprocess.run(["magiskboot", "unpack", image], cwd=folder).returncode
    if code not in (0, 3):
        raise ValueError(f"magiskboot failed to unpack {image.name} ({code})")


def vendor_boot(image, work, avb, decrypt, writable=None):
    with TemporaryDirectory(dir=work) as tmp:
        folder = Path(tmp)
        unpack(image, folder)
        changed = False
        for cpio in folder.rglob("*.cpio"):
            updates = []
            with libarchive.file_reader(str(cpio)) as archive:
                for entry in archive:
                    if not entry.isfile or not Path(entry.pathname).name.startswith("fstab."):
                        continue
                    original = b"".join(entry.get_blocks())
                    patched = patch_fstab(original, avb, decrypt, writable)
                    if patched == original:
                        continue
                    if entry.uid or entry.gid or any(c.isspace() for c in entry.pathname):
                        raise ValueError(f"Unsupported fstab metadata: {entry.pathname}")
                    item = folder / f"fstab-{len(updates)}"
                    item.write_bytes(patched)
                    updates.append(f"add {stat.S_IMODE(entry.mode):o} {entry.pathname} {item.name}")
            if updates:
                run("magiskboot", "cpio", cpio, *updates, cwd=folder)
                changed = True
        if changed:
            output = folder / "patched.img"
            run("magiskboot", "repack", image, output, cwd=folder)
            if output.stat().st_size > image.stat().st_size:
                raise ValueError("Patched vendor_boot exceeds stock capacity")
            output.replace(image)


def patch_ksu(images, target, work):
    if target not in ("boot", "init_boot"):
        raise ValueError(f"Unsupported KernelSU target: {target}")
    with TemporaryDirectory(dir=work) as tmp:
        folder = Path(tmp)
        kmi = os.getenv("KSU_KMI")
        if not kmi:
            unpack(images / "boot.img", folder)
            match = re.search(rb"Linux version (\d+\.\d+)\.\d+[^\s]*?-(android\d+)(?=[-\s])",
                              (folder / "kernel").read_bytes())
            if not match:
                raise ValueError("Stock kernel has no Android KMI string. Set KSU_KMI in the device profile.")
            kmi = (match[2] + b"-" + match[1]).decode()
        image = images / f"{target}.img"
        run("ksud", "boot-patch", "--boot", image, "--kmi", kmi, "--arch", "aarch64",
            "--out", folder, "--out-name", "patched.img")
        patched = folder / "patched.img"
        if not 0 < patched.stat().st_size <= image.stat().st_size:
            raise ValueError(f"Invalid patched {target} size")
        # Verify the LKM made it into the actual output before replacing the stock image.
        verify = folder / "verify"
        verify.mkdir()
        unpack(patched, verify)
        run("magiskboot", "cpio", "ramdisk.cpio", "exists kernelsu.ko", cwd=verify)
        patched.replace(image)


def disable_xiaomi_hashtree(image):
    # AvbVBMetaImageHeader.flags is a big-endian uint32 at offset 120.
    # Keep the OEM key, descriptors, rollback fields and padding in place.
    run("avbtool", "info_image", "--image", image, stdout=subprocess.DEVNULL)
    with image.open("r+b") as f:
        if f.read(4) != b"AVB0":
            raise ValueError(f"Expected standalone vbmeta header in {image}")
        f.seek(120)
        flags = int.from_bytes(f.read(4), "big")
        f.seek(120)
        f.write((flags | 1).to_bytes(4, "big"))


def disable_avb(image):
    info = run("avbtool", "info_image", "--image", image, text=True, capture_output=True).stdout
    def field(key):
        return re.search(rf"^{key}:\s+(\d+)", info, re.M)[1]
    out = image.with_suffix(".new")
    run("avbtool", "make_vbmeta_image", "--algorithm", "NONE", "--flags", "3",
        "--include_descriptors_from_image", image, "--rollback_index", field("Rollback Index"),
        "--rollback_index_location", field("Rollback Index Location"), "--output", out)
    if out.stat().st_size > image.stat().st_size:
        raise ValueError(f"vbmeta exceeds stock size: {image}")
    with out.open("ab") as f:
        f.truncate(image.stat().st_size)
    run("avbtool", "info_image", "--image", out, stdout=subprocess.DEVNULL)
    out.replace(image)
