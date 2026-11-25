"""Edit mounted stock filesystems through private, per-partition overlays."""
import errno
import os
import re
import shutil
import stat
import subprocess
import tomllib
import zipfile
from contextlib import ExitStack
from itertools import chain
from pathlib import Path
from tempfile import TemporaryDirectory

from super_image import run


def mount(stack, target, *args):
    target.mkdir(parents=True)
    run("mount", *args, target)
    stack.callback(run, "umount", target)


SYSTEM_ENTRY = re.compile(
    rb"(?P<prefix>[ \t]*(?P<src>[^#\s]+)[ \t]+(?P<point>/(?:system|product|system_ext))[ \t]+)"
    rb"(?P<fs>ext4|erofs|f2fs)(?P<gap>[ \t]+)(?P<options>\S+)(?P<tail>[ \t]+)"
    rb"(?P<flags>[^\s#]+)(?P<comment>[ \t]*(?:#[^\r\n]*)?)"
)


def complete_system_fstab(data, preferred):
    """Keep each set of fs_mgr alternatives consecutive, with the requested format first."""
    lines = data.split(b"\n")
    groups = {}
    for index, line in enumerate(lines):
        entry = SYSTEM_ENTRY.fullmatch(line.removesuffix(b"\r"))
        if not entry:
            continue
        flags = set(entry["flags"].split(b","))
        if b"logical" in flags:
            scope = flags & {b"first_stage_mount", b"latemount", b"slotselect",
                             b"slotselect_other", b"recoveryonly"}
            key = entry["src"], entry["point"], frozenset(scope)
            groups.setdefault(key, []).append((index, entry))

    for (_, point, _), entries in groups.items():
        template = entries[0][1]
        first = entries[0][0]
        carriage = b"\r" if lines[first].endswith(b"\r") else b""
        target = preferred.get(point[1:].decode(), "").encode()
        candidates = []
        present = set()
        for index, entry in entries:
            fs = entry["fs"]
            line = entry[0]
            modes = entry["options"].split(b",")
            if fs == target or (fs == b"erofs" and (b"rw" in modes or b"ro" not in modes)):
                mode = b"ro" if fs == b"erofs" else b"rw"
                options = b",".join([mode, *(o for o in modes if o not in (b"ro", b"rw"))])
                line = line[:entry.start("options")] + options + line[entry.end("options"):]
            candidates.append((fs, line))
            present.add(fs)
            lines[index] = None
        for fs in (b"ext4", b"erofs", b"f2fs"):
            if fs in present:
                continue
            # Carry portable VFS options only. EROFS, ext4 and F2FS have distinct mount options.
            mode = b"rw" if fs == target and fs != b"erofs" else b"ro"
            options = [o for o in template["options"].split(b",") if o in
                       (b"nodev", b"nosuid", b"noexec", b"noatime", b"nodiratime", b"relatime",
                        b"sync", b"dirsync")]
            if fs != b"erofs" and b"discard" in template["options"].split(b","):
                options.append(b"discard")
            flags = [f for f in template["flags"].split(b",") if f not in
                     (b"checkpoint=fs", b"ext_meta_csum", b"fscompress", b"atgc", b"formattable")]
            line = (template["prefix"] + fs + template["gap"] + b",".join([mode, *options])
                    + template["tail"] + b",".join(flags))
            candidates.append((fs, line))
        if target:
            candidates.sort(key=lambda item: item[0] != target)
        lines[first] = (carriage + b"\n").join(line for _, line in candidates) + carriage
    return b"\n".join(line for line in lines if line is not None)


def patch_fstab(data, avb, decrypt, writable=None):
    # Only fs_mgr flags (field five) have these meanings. Preserve spacing and comments.
    def line(match):
        flags = match[4].split(b",")
        remove = {b"avb", b"avb_keys", b"verify", b"verifyatboot"} if avb else set()
        if decrypt and match[2] == b"/data":
            remove |= {b"fileencryption", b"metadata_encryption", b"forceencrypt",
                       b"forcefdeorfbe", b"keydirectory", b"wrappedkey", b"inlinecrypt_optimized"}
        keep = [f for f in flags if f.partition(b"=")[0] not in remove]
        return match[1] + match[2] + match[3] + (b",".join(keep) or b"defaults")
    data = re.sub(rb"(?m)^([ \t]*[^#\s]+[ \t]+)(\S+)([ \t]+\S+[ \t]+\S+[ \t]+)([^\s#]+)", line, data)
    return complete_system_fstab(data, writable) if writable is not None else data


def remove_task(rom, rel):
    path = rom / rel
    if Path(rel).is_absolute() or ".." in Path(rel).parts or len(Path(rel).parts) < 2:
        raise ValueError(f"Invalid debloat path: {rel}")
    if not path.parent.resolve().is_relative_to(rom):
        raise ValueError(f"Debloat path escapes ROM: {rel}")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def customize(rom, profile, scripts, fixes, debloat, inject, avb, decrypt, writable=None):
    # Firmware symlinks may be absolute. Give device hooks write access only to ROM mounts.
    sandbox = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev",
               "--proc", "/proc", "--tmpfs", "/tmp", "--bind", rom, rom]
    if bundle := os.environ.get("YUCCA_GPI_BUNDLE_DIR"):
        sandbox.extend(["--ro-bind", bundle, bundle])
    for fix in fixes.split():
        run(*sandbox, "python3", profile / "fixes" / fix, rom)
    if debloat:
        with (profile / "CONFIGS/tasks.toml").open("rb") as f:
            for task in tomllib.load(f)["tasks"]:
                if task["action"] == "remove":
                    remove_task(rom, task["path"])
    if profile.name == "xiaomi":
        if os.getenv("APPLY_EXTRA_PROPS", "true") == "true":
            run(*sandbox, "bash", scripts / "patch_props.sh", rom, profile / "CONFIGS/extra_props.txt")
        if inject:
            data_apps = Path("product/data-app")
            source = profile / "apks" / data_apps
            if not source.is_dir():
                raise ValueError(f"Missing fetched profile APKs in {source}")
            run(*sandbox, "bash", scripts / "inject_apks.sh", rom / data_apps, source)
            run(*sandbox, "python3", Path(__file__).with_name("fetch_cyime.py"),
                rom / data_apps / "CyIME/CyIME.apk")
    for fstab in rom.rglob("fstab.*"):
        if not fstab.is_symlink() and fstab.is_file():
            original = fstab.read_bytes()
            patched = patch_fstab(original, avb, decrypt, writable)
            if patched != original:
                fstab.write_bytes(patched)
    # Remove app precompiles only when ART can regenerate them from the on-image dex.
    for oat in rom.rglob("oat"):
        if oat.is_symlink() or not oat.is_dir():
            continue
        apps = list(oat.parent.glob("*.apk"))
        if apps and all(has_dex(app, rom) for app in apps):
            shutil.rmtree(oat)


def has_dex(app, rom):
    if not app.resolve().is_relative_to(rom):
        return False
    try:
        with zipfile.ZipFile(app) as z:
            return "classes.dex" in z.namelist()
    except zipfile.BadZipFile:
        return False


def xattr(path, key):
    try:
        return os.getxattr(path, key, follow_symlinks=False)
    except OSError as error:
        if error.errno not in (errno.ENODATA, errno.ENOENT, errno.ENOTDIR):
            raise
        return None


def restore_metadata(view, lower, upper, contexts, android_root):
    # The overlay root itself inherits metadata from the newly created upper directory.
    for changed in chain((upper,), upper.rglob("*")):
        rel = changed.relative_to(upper)
        dst, src = view / rel, lower / rel
        if not os.path.lexists(dst):  # overlay whiteout
            continue
        if os.path.lexists(src):
            old, new = src.lstat(), dst.lstat()
            if stat.S_IFMT(old.st_mode) == stat.S_IFMT(new.st_mode):
                if (old.st_uid, old.st_gid) != (new.st_uid, new.st_gid):
                    os.chown(dst, old.st_uid, old.st_gid, follow_symlinks=False)
                if not dst.is_symlink() and stat.S_IMODE(old.st_mode) != stat.S_IMODE(dst.lstat().st_mode):
                    dst.chmod(stat.S_IMODE(old.st_mode))
        for key in ("security.selinux", "security.capability"):
            value = xattr(src, key)
            if key == "security.selinux" and not value and not xattr(dst, key):
                path = str(android_root / rel)
                if contexts:
                    lookup = subprocess.run(["matchpathcon", "-N", "-n", "-f", contexts, path],
                                            capture_output=True, text=True)
                    label = lookup.stdout.strip()
                    if lookup.returncode == 0 and ":object_r:" in label:
                        value = label.encode() + b"\0"
                if not value and xattr(lower, key):
                    raise ValueError(f"No Android SELinux file context for {path}")
            if value and xattr(dst, key) != value:
                os.setxattr(dst, key, value, follow_symlinks=False)


def make_fs(fs, view, output, label, image, compression):
    uuid = subprocess.run(["blkid", "-p", "-s", "UUID", "-o", "value", image],
                          capture_output=True, text=True).stdout.strip()
    if fs == "erofs":
        run("mkfs.erofs", "-z" + compression, *(["-U", uuid] if uuid else []),
            *(["-L", label] if label else []), output, view)
        run("fsck.erofs", "--extract", output)
    elif fs == "ext4":
        used = int(run("du", "-s", "-B1", "--apparent-size", view, capture_output=True).stdout.split()[0])
        count = sum(1 for _ in view.rglob("*")) + 512
        size = max(16 << 20, used + used // 8 + count * 8192 + (8 << 20))
        with output.open("wb") as f:
            f.truncate((size + 4095) // 4096 * 4096)
        run("mke2fs", "-q", "-t", "ext4", "-b", "4096", "-m", "0", "-N", str(count),
            "-O", "^orphan_file,^metadata_csum_seed", "-d", view, "-L", label,
            *(["-U", uuid] if uuid else []), output)
        # mke2fs -d populates children but can give inode 2 host-default metadata.
        with ExitStack() as stack:
            root = Path(stack.enter_context(TemporaryDirectory(dir=output.parent))) / "root"
            mount(stack, root, "-o", "loop,nodev,nosuid", output)
            st = view.stat()
            os.chown(root, st.st_uid, st.st_gid)
            shutil.copystat(view, root)
            if xattr(root, "security.selinux") != xattr(view, "security.selinux"):
                raise ValueError(f"Failed to preserve the ext4 root SELinux label in {output}")
        run("e2fsck", "-fp", output)
        run("resize2fs", "-M", output)
        run("e2fsck", "-fn", output)
    else:
        raise ValueError(f"Unsupported target filesystem {fs}")


def f2fs_sizes(images, alignment):
    """Share unused ext4 blocks within one LP group before creating F2FS images."""
    result = {}
    used = {}
    spare_units = 0
    for image in images:
        header = run("tune2fs", "-l", image, capture_output=True, text=True,
                     env={**os.environ, "LC_ALL": "C"}).stdout
        fields = dict(line.split(":", 1) for line in header.splitlines() if ":" in line)
        used[image] = (int(fields["Block count"]) - int(fields["Free blocks"])) * int(fields["Block size"])
        size = image.stat().st_size
        units = (size - used[image]) // alignment
        spare_units += units
        # Transfer whole alignment units so LP padding and total group usage stay identical.
        result[image] = size - units * alignment
    weight = sum(used.values())
    # Each filesystem needs its own metadata and GC reserve. Data blocks scale with content.
    common = spare_units // 2
    weighted = spare_units - common
    for index, image in enumerate(images):
        left = len(images) - index
        fixed_share = common // left
        variable_share = weighted * used[image] // weight
        result[image] += (fixed_share + variable_share) * alignment
        common -= fixed_share
        weighted -= variable_share
        weight -= used[image]
    return result


def convert_to_f2fs(image, size=None):
    output = image.with_suffix(".f2fs")
    with output.open("xb") as file:
        file.truncate(image.stat().st_size if size is None else size)
    uuid = run("blkid", "-p", "-s", "UUID", "-o", "value", image, text=True, capture_output=True).stdout.strip()
    run("mkfs.f2fs", "-f", "-t", "0", "-U", uuid, output)
    # The appliance kernel supports security xattrs even on hosts whose F2FS module does not.
    src = "/.yucca-source"
    result = run("guestfish", "--format=raw", "-a", output, "add-drive-opts", image,
                 "readonly:true", "format:raw", ":", "run", ":", "mount", "/dev/sda", "/",
                 ":", "mkdir", src, ":", "mount-ro", "/dev/sdb", src,
                 ":", "getxattr", src, "security.selinux", ":", "echo", "",
                 ":", "cp-a", src + "/.", "/", ":", "umount", src, ":", "rmdir", src,
                 ":", "getxattr", "/", "security.selinux", ":", "sync", stdout=subprocess.PIPE,
                 env={**os.environ, "LIBGUESTFS_BACKEND": "direct"})
    labels = result.stdout.splitlines()
    if len(labels) != 2 or not labels[0] or labels[0] != labels[1]:
        raise ValueError(f"F2FS root SELinux label mismatch: {image}")
    run("fsck.f2fs", "--dry-run", "-f", output)
    output.replace(image)


def edit(info, images, work, callback, extract_only=False, writable_targets=()):
    rom = work / "rom"
    rom.mkdir()
    entries = []
    rebuilt_ext4 = []
    with ExitStack() as stack:
        for n, (_, _, source) in info["partitions"].items():
            if not source:
                continue
            base = n[:-2] if n.endswith(("_a", "_b")) else n
            slot_a = info["partitions"].get(base + "_a")
            if n.endswith("_b") and slot_a and slot_a[2]:
                continue  # retain stock populated B slots verbatim
            image = images / f"{source}.img"
            fs = subprocess.run(["blkid", "-p", "-o", "value", "-s", "TYPE", image],
                                capture_output=True, text=True).stdout.strip()
            if fs not in {"erofs", "ext4"}:
                if base in ("system", "vendor", "product", "system_ext", "odm"):
                    raise ValueError(f"Unsupported {base} filesystem: {fs}")
                continue
            lower, upper, scratch = (work / d / base for d in ("lower", "upper", "overlay-work"))
            view = rom / base
            mount(stack, lower, "-t", fs, "-o", "ro,loop,nodev,nosuid" + (",noload" if fs == "ext4" else ""), image)
            upper.mkdir(parents=True)
            scratch.mkdir(parents=True)
            mount(stack, view, "-t", "overlay", "overlay", "-o",
                  f"lowerdir={lower},upperdir={upper},workdir={scratch},nodev,nosuid")
            entries.append((n, base, image, fs, view, lower, upper))
        callback(rom)
        if extract_only:
            return
        order = {"plat_file_contexts": 0, "system_ext_file_contexts": 1,
                 "product_file_contexts": 2, "vendor_file_contexts": 3, "odm_file_contexts": 4}
        policy_files = sorted(
            (p for p in rom.glob("*/**/etc/selinux/*_file_contexts")
             if not p.is_symlink() and p.is_file()),
            key=lambda p: (order.get(p.name, 99), str(p)),
        )
        contexts = None
        if policy_files:
            contexts = work / "merged_file_contexts"
            contexts.write_bytes(b"\n".join(p.read_bytes().rstrip(b"\n") for p in policy_files) + b"\n")
        for n, base, image, fs, view, lower, upper in entries:
            target = os.getenv(n.upper() + "_FS") or os.getenv(base.upper() + "_FS") or fs
            if target == "f2fs" and n not in writable_targets:
                raise ValueError(f"Unsupported F2FS target: {n}")
            if target == fs and n not in writable_targets and not any(upper.iterdir()):
                continue
            android_root = Path("/") if base == "system" and (view / "system").is_dir() else Path("/") / base
            restore_metadata(view, lower, upper, contexts, android_root)
            output = images / f"{base}.new"
            label = subprocess.run(["blkid", "-p", "-s", "LABEL", "-o", "value", image],
                                   capture_output=True, text=True).stdout.strip()
            compression = (os.getenv(n.upper() + "_EROFS_COMPRESSION")
                           or os.getenv(base.upper() + "_EROFS_COMPRESSION")
                           or "lz4hc")
            # The ext4 staging image preserves Android metadata and measures the populated tree.
            make_fs("ext4" if target == "f2fs" else target, view, output, label, image, compression)
            output.replace(image)
            if target in {"ext4", "f2fs"} and base in {"system", "product", "system_ext"}:
                rebuilt_ext4.append(n)
    return rebuilt_ext4
