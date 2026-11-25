"""Shared local and CI build entry point. Run in a private Linux mount namespace."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import zipfile

from boot import disable_avb, disable_xiaomi_hashtree, patch_ksu, vendor_boot
from filesystems import convert_to_f2fs, customize, edit, f2fs_sizes
from super_image import grow_xiaomi_ext4, name, ota, repack, run, samsung


def enabled(key, default="true"):
    value = os.getenv(key, default).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{key} must be true or false")
    return value == "true"


def xiaomi_zip(z, images, scripts, tools, work, ksu):
    compressed = work / "super.img.zst"
    run("zstd", "-T0", "-1", images / "super.img", "-o", compressed)
    (images / "super.img").unlink()
    z.write(compressed, compressed.name)
    compressed.unlink()
    for img in sorted(images.glob("*.img")):
        if img.name != "super.img":
            z.write(img, "images/" + img.name, zipfile.ZIP_DEFLATED, compresslevel=1)
    for item in ("flash_all.bat", "flash_all_except_storage.bat", "Tool.bat"):
        z.write(scripts / item, item)
    for item in ("update-binary", "updater-script"):
        z.write(scripts / "flash_package" / item, "META-INF/com/google/android/" + item)
    z.write(tools / "zstd_arm64", "META-INF/zstd_arm64")
    for item in ("adb.exe", "fastboot.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "zstd.exe"):
        z.write(tools / item, item)
    if ksu:
        z.write(tools / "KernelSU.apk", "KernelSU.apk")


def write_odin_tar(z, stock_member, dest, replacements, skip=()):
    """Stream untouched entries from stock and stage at most one compressed replacement."""
    pending = dict(replacements)

    def add(tar, member, path):
        compressed = dest.with_suffix(".lz4")
        try:
            if member.name.endswith(".lz4"):
                run("lz4", "-q", "-9", "-B6", "--content-size", "--no-frame-crc", path, compressed)
                path = compressed
            member.size = path.stat().st_size
            with path.open("rb") as f:
                tar.addfile(member, f)
        finally:
            compressed.unlink(missing_ok=True)

    with z.open(stock_member) as src, tarfile.open(fileobj=src, mode="r|") as original, \
            tarfile.open(dest, "w|", format=tarfile.GNU_FORMAT) as tar:
        for member in original:
            key = Path(member.name).name.removesuffix(".lz4")
            if key in skip:
                continue
            if member.isfile() and key in replacements:
                add(tar, member, pending.pop(key))
            else:
                tar.addfile(member, original.extractfile(member) if member.isfile() else None)
        for key, path in pending.items():
            # BL may carry the only vbmeta, and the PIT usually lives in CSC.
            add(tar, tarfile.TarInfo(key + ".lz4" if key.endswith(".img") else key), path)


def odin_files(stock, images, work):
    with zipfile.ZipFile(stock) as z:
        tars = [n for n in z.namelist() if n.endswith((".tar", ".tar.md5")) and not n.startswith("__MACOSX/")]
        ap, = [n for n in tars if Path(n).name.startswith("AP_")]
        pda = name(Path(ap).name.split("_")[1])
        csc, = [n for n in tars if Path(n).name.startswith("CSC_")]
        home, = [n for n in tars if Path(n).name.startswith("HOME_CSC_")]
        with z.open(csc) as f, tarfile.open(fileobj=f, mode="r|") as tar:
            for member in tar:
                if member.isfile() and member.name.lower().endswith(".pit"):
                    pit = work / name(Path(member.name).name)
                    with pit.open("xb") as out:
                        shutil.copyfileobj(tar.extractfile(member), out)
                    break
            else:
                raise ValueError("CSC contains no PIT")
        files = [work / f"{kind}_{pda}_By.Yucca.tar" for kind in ("AP", "CSC")]
        write_odin_tar(z, ap, files[0], {p.name: p for p in images.glob("*.img")}, skip={"userdata.img"})
        write_odin_tar(z, home, files[1], {pit.name: pit})
    return f"{pda}_By.Yucca.zip", files


def build(args, work):
    profile = args.profiles / args.brand
    scripts = args.profiles / "scripts"
    images = work / "images"
    images.mkdir()
    is_xiaomi = args.brand == "xiaomi"
    if is_xiaomi:
        info = ota(args.zip, images, args.device, int(os.getenv("SUPER_DEVICE_SIZE") or 0))
    else:
        info = samsung(args.zip, images, work, os.getenv("MODEL"), system_only=bool(args.gpi_out))
    if args.gpi_out:
        args.gpi_out.mkdir(parents=True, exist_ok=True)
        edit(info, images, work, lambda rom: run("python3", profile / "scripts/extract_gpi_from_tgy.py",
                                                 rom, args.gpi_out), extract_only=True)
        return
    avb, decrypt = enabled("REMOVE_AVB"), enabled("REMOVE_ENCRYPT")
    writable = {}
    if is_xiaomi and avb:
        for base in ("system", "product", "system_ext"):
            target = os.getenv(base.upper() + "_A_FS") or os.getenv(base.upper() + "_FS")
            if target in {"ext4", "f2fs"}:
                writable[base] = target
                part = base + "_a"
                group, _, source = info["partitions"][part]
                info["partitions"][part] = (group, "none", source)
    if not avb and (images / "vbmeta.img").exists():
        raise ValueError("Verified builds require OEM signing keys; this builder supports unlocked devices")
    fstab_formats = writable if is_xiaomi else None  # An empty map keeps the OEM candidate order.
    rebuilt_ext4 = edit(info, images, work, lambda rom: customize(rom, profile, scripts, os.getenv("FIX_SCRIPTS", ""),
         enabled("DEBLOAT") and not args.skip_debloat, not args.skip_apks, avb, decrypt, fstab_formats),
         writable_targets={base + "_a" for base in writable})
    if (is_xiaomi or avb or decrypt) and (images / "vendor_boot.img").exists():
        vendor_boot(images / "vendor_boot.img", work, avb, decrypt, fstab_formats)
    if args.swap_kernel:
        out = images / "replacement.img"
        run("python3", profile / "scripts/replace_boot_kernel.py", "--series", os.environ["SERIES"],
            "--boot-img", images / "boot.img", "--output", out, "--magiskboot", args.tools / "magiskboot")
        out.replace(images / "boot.img")
    ksu = enabled("KSU_PATCH") and not args.skip_ksu
    if ksu:
        patch_ksu(images, os.getenv("KSU_INPUT", "init_boot"), work)
    if avb:
        if not (images / "vbmeta.img").is_file():
            raise ValueError("Missing top-level vbmeta.img")
        if is_xiaomi:
            disable_xiaomi_hashtree(images / "vbmeta.img")
        else:
            for img in images.glob("vbmeta*.img"):
                disable_avb(img)
    if is_xiaomi and rebuilt_ext4:
        grow_xiaomi_ext4(info, images, rebuilt_ext4, work)
        for group in info["groups"]:
            targets = [images / f"{base}.img" for base, target in writable.items()
                       if target == "f2fs" and info["partitions"][base + "_a"][0] == group]
            for image, size in f2fs_sizes(targets, info["alignment"]).items():
                print(f"{image.stem}: F2FS {size} bytes", flush=True)
                convert_to_f2fs(image, size)
    repack(info, images, images / "super.img", sparse=not is_xiaomi)
    for n, (_, _, source) in info["partitions"].items():
        (images / f"{source or n}.img").unlink(missing_ok=True)

    if is_xiaomi:
        version = args.version or (re.search(r"OS[\d.]+\.\w+", args.zip.name) or ["local"])[0]
        filename = f"YuccaROM_{args.device}_{name(version)}_{datetime.now():%Y%m%d}.zip"
    else:
        filename, files = odin_files(args.zip, images, work)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=args.out, prefix=".building-", delete=False) as tmp:
        partial = Path(tmp.name)
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as z:
            if is_xiaomi:
                xiaomi_zip(z, images, scripts, args.tools, work, ksu)
            else:
                for file in files:
                    z.write(file, file.name, zipfile.ZIP_DEFLATED, compresslevel=1)
        archive = args.out / filename
        partial.chmod(0o644)
        partial.replace(archive)
    finally:
        partial.unlink(missing_ok=True)
    print(archive, flush=True)
    if args.result:
        args.result.write_text(str(archive) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--brand", choices=("xiaomi", "Samsung"), default="xiaomi")
    p.add_argument("--profiles", type=Path, required=True)
    p.add_argument("--tools", type=Path, required=True)
    p.add_argument("--zip", type=Path, required=True)
    p.add_argument("--device", type=name, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--result", type=Path)
    p.add_argument("--gpi-out", type=Path)
    p.add_argument("--version")
    p.add_argument("--skip-ksu", action="store_true")
    p.add_argument("--skip-debloat", action="store_true")
    p.add_argument("--skip-apks", action="store_true")
    p.add_argument("--swap-kernel", action="store_true")
    args = p.parse_args()
    for key in ("zip", "out", "tools", "profiles", "result", "gpi_out"):
        if value := getattr(args, key):
            setattr(args, key, value.resolve())
    with tempfile.TemporaryDirectory(prefix="yucca-", dir=os.getenv("ROM_WORK_DIR", "/var/tmp")) as tmp:
        os.environ["TMPDIR"] = tmp
        build(args, Path(tmp))


if __name__ == "__main__":
    main()
