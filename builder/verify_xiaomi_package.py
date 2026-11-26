"""Verify the shared Fastboot and Recovery package before staging a release."""

import argparse
from pathlib import Path
import struct
import zipfile


def verify(archive, device):
    clear = "flash_all.bat"
    preserve = "flash_all_except_storage.bat"
    with zipfile.ZipFile(archive) as package, archive.open("rb") as raw:
        names = package.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate members in flash package")
        flashers = {name for name in names if name.lower().startswith("flash") and name.lower().endswith(".bat")}
        if flashers != {clear, preserve} or any(
            name.lower().endswith(".ps1") or name.lower() == "flash_product.txt" for name in names
        ):
            raise ValueError("Expected exactly two embedded Windows flash entry points")

        marker = b"\r\n# YUCCAROM_POWERSHELL\r\n"
        clear_launcher, _, clear_body = package.read(clear).partition(marker)
        preserve_launcher, _, preserve_body = package.read(preserve).partition(marker)
        if (
            not clear_body
            or clear_body != preserve_body
            or b" -Wipe " not in clear_launcher
            or b" -Wipe " in preserve_launcher
        ):
            raise ValueError("Windows flashers have inconsistent implementations or wipe modes")

        for name in (
            "adb.exe", "fastboot.exe", "zstd.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll",
            "META-INF/com/google/android/update-binary", "META-INF/com/google/android/updater-script",
            "META-INF/zstd_arm64",
        ):
            if package.getinfo(name).file_size == 0:
                raise ValueError(f"Empty package member: {name}")

        version, codename, super_bytes, *firmware = package.read(
            "META-INF/yucca/flash_manifest.txt"
        ).decode("ascii").splitlines()
        if version != "YUCCA_FLASH_V1" or codename != device or int(super_bytes) <= 0:
            raise ValueError("Incorrect device or super size in flash manifest")
        images = [
            f"{Path(entry.filename).stem} {entry.file_size}"
            for entry in package.infolist()
            if entry.filename.startswith("images/") and entry.filename.endswith(".img")
        ]
        if not images or sorted(firmware) != sorted(images):
            raise ValueError("Firmware members disagree with the flash manifest")

        offset, size = map(int, package.read("META-INF/yucca/super_range.txt").split())
        member = package.getinfo("super.img.zst")
        raw.seek(member.header_offset)
        header = raw.read(30)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            raise ValueError("Invalid local header for super.img.zst")
        name_length, extra_length = struct.unpack_from("<HH", header, 26)
        if (
            member.compress_type != zipfile.ZIP_STORED
            or size != member.file_size
            or offset != raw.tell() + name_length + extra_length
            or offset + size > archive.stat().st_size
        ):
            raise ValueError("Recovery byte range disagrees with super.img.zst")
        raw.seek(offset)
        if raw.read(4) != b"\x28\xb5\x2f\xfd":
            raise ValueError("Recovery byte range does not start with a Zstandard frame")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("device")
    args = parser.parse_args()
    try:
        verify(args.archive, args.device)
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Invalid flash package: {exc}\n")
    print(f"Verified Fastboot and Recovery package for {args.device}: {args.archive.name}")


if __name__ == "__main__":
    main()
