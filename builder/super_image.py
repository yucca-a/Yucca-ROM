"""Stock firmware containers and Android logical partition metadata."""
import hashlib
import json
import re
import shutil
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(f"Invalid partition or group name {value!r}")
    return value


def ota(archive, images, device, size):
    from update_metadata_pb2 import DeltaArchiveManifest
    with zipfile.ZipFile(archive) as z:
        metadata = dict(line.split("=", 1) for line in
                        z.read("META-INF/com/android/metadata").decode().splitlines() if "=" in line)
        if device not in metadata.get("pre-device", "").split("|"):
            raise ValueError(f"OTA pre-device does not match {device}")
        with z.open("payload.bin") as src:
            magic, version, length, _ = struct.unpack(">4sQQI", src.read(24))
            if magic != b"CrAU" or version != 2 or length > 128 << 20:
                raise ValueError("Invalid full OTA payload header")
            manifest = DeltaArchiveManifest.FromString(src.read(length))
    dm = manifest.dynamic_partition_metadata
    if manifest.partial_update or manifest.minor_version or not dm.snapshot_enabled or not dm.groups:
        raise ValueError("Expected a full virtual A/B OTA with dynamic partition groups")
    groups, partitions = {}, {}
    for group in dm.groups:
        for suffix in ("_a", "_b"):
            group_name = name(group.name) + suffix
            groups[group_name] = group.size
            for part in group.partition_names:
                partspec = (group_name, "readonly", name(part) if suffix == "_a" else None)
                partitions[name(part) + suffix] = partspec
    if size <= 0 or size % 4096:
        raise ValueError("Set SUPER_DEVICE_SIZE to the stock super partition size in bytes")
    # Validate all names before letting the extractor create paths.
    for part in manifest.partitions:
        name(part.partition_name)
    run("payload-dumper-go", "-q", "-c", "4", "-o", images, archive)
    for part in manifest.partitions:
        img = images / (part.partition_name + ".img")
        with img.open("rb") as f:
            info = part.new_partition_info
            if img.stat().st_size != info.size or hashlib.file_digest(f, "sha256").digest() != info.hash:
                raise ValueError(f"Payload partition failed size/hash verification: {img.name}")
    return layout(size, groups, partitions, 65536, 3, True)


def layout(size, groups, partitions, metadata_size, slots, virtual, alignment=1048576,
           offset=0, block=4096):
    return dict(size=size, groups=groups, partitions=partitions, metadata_size=metadata_size,
                slots=slots, virtual=virtual, alignment=alignment, offset=offset, block=block)


def stock_layout(raw):
    dump = run("lpdump", raw, capture_output=True, text=True).stdout
    meta = json.loads(run("lpdump", "--json", raw, capture_output=True, text=True).stdout)
    dev, = meta["block_devices"]
    size = int(dev["size"])
    if dev["name"] != "super" or raw.stat().st_size != size:
        raise ValueError("Unsupported multi-device or truncated super image")
    attrs = dict(re.findall(r"  Name: (\S+)\n  Group: [^\n]+\n  Attributes: ([^\n]+)", dump))
    groups = {name(g["name"]): int(g.get("maximum_size", 0)) for g in meta["groups"]}
    partitions = {}
    for p in meta["partitions"]:
        n = name(p["name"])
        attr = attrs[n]
        if attr not in {"none", "readonly"}:
            raise ValueError(f"Unsupported attributes for {n}: {attr}")
        partitions[n] = (p["group_name"], attr, n if int(p.get("size", 0)) else None)
    def field(label):
        return int(re.search(rf"^{label}: (\d+)", dump, re.M)[1])
    flags = re.search(r"^Header flags: (.*)", dump, re.M)
    if flags and flags[1] not in ("none", "virtual_ab_device", ""):
        raise ValueError(f"Unsupported super flags: {flags[1]}")
    if re.search(r"^  Flags: (?!none$)", dump, re.M):
        raise ValueError("Slot-suffixed devices/groups require a different super layout")
    return layout(size, groups, partitions, field("Metadata max size"), field("Metadata slot count"),
                  bool(flags and flags[1] == "virtual_ab_device"), int(dev.get("alignment", 0)),
                  int(dev.get("alignment_offset", 0)), int(dev["block_size"]))


def samsung(archive, images, work, model=None, system_only=False):
    with zipfile.ZipFile(archive) as z:
        tars = [n for n in z.namelist() if n.endswith((".tar", ".tar.md5")) and not n.startswith("__MACOSX/")]
        ap, = [n for n in tars if Path(n).name.startswith("AP_")]
        if model and not Path(ap).name.startswith("AP_" + model.removeprefix("SM-")):
            raise ValueError(f"{ap} does not match {model}")
        # Samsung may put vbmeta in BL. The Odin packager expects it alongside AP images.
        archives = [ap]
        if not system_only:
            archives.extend(n for n in tars if Path(n).name.startswith("BL_"))
        for filename in archives:
            with z.open(filename) as src, tarfile.open(fileobj=src, mode="r|") as tar:
                for member in tar:
                    base = Path(member.name).name
                    stem = base.removesuffix(".lz4")
                    if system_only and stem != "super.img":
                        continue
                    if not member.isfile() or stem not in {
                        "super.img", "boot.img", "init_boot.img", "vendor_boot.img",
                        "vbmeta.img", "vbmeta_system.img"
                    } or (filename != ap and stem != "vbmeta.img"):
                        continue
                    dest = images / stem
                    if dest.exists():
                        continue
                    with tar.extractfile(member) as f, dest.open("xb") as out:
                        if base.endswith(".lz4"):
                            # ExFileObject isn't a seekable OS descriptor. Feed the decoder a pipe.
                            with subprocess.Popen(["lz4", "-dc"], stdin=subprocess.PIPE, stdout=out) as proc:
                                with proc.stdin:
                                    shutil.copyfileobj(f, proc.stdin, 1 << 20)
                                if proc.wait():
                                    raise ValueError(f"LZ4 decode failed: {base}")
                        else:
                            shutil.copyfileobj(f, out, 1 << 20)
    raw = images / "super.img"
    with raw.open("rb") as f:
        sparse = f.read(4) == b"\x3a\xff\x26\xed"
    if sparse:
        run("simg2img", raw, work / "raw.img")
        (work / "raw.img").replace(raw)
    info = stock_layout(raw)
    if system_only:
        system = next((n for n in ("system", "system_a", "system_b")
                       if n in info["partitions"] and info["partitions"][n][2]), None)
        if system is None:
            raise ValueError("No populated system partition for GPI extraction")
        run("lpunpack", "-p", system, raw, images)
        info["partitions"] = {system: info["partitions"][system]}
    else:
        run("lpunpack", raw, images)
    raw.unlink()
    return info


def write_super(info, images, output, sparse, metadata_only=False):
    args = ["lpmake", "--metadata-size", str(info["metadata_size"]), "--metadata-slots", str(info["slots"]),
            "--device", "super:{size}:{alignment}:{offset}".format(**info),
            "--block-size", str(info["block"]), "--output", output]
    if info["virtual"]:
        args.append("--virtual-ab")
    if sparse:
        args.append("--sparse")
    elif not metadata_only:
        # liblp writes full geometry and both copies of every metadata slot.
        args.append("--force-full-image")
    for group, maximum in info["groups"].items():
        if group != "default":
            args.extend(["--group", f"{group}:{maximum}"])
    for n, (group, attr, source) in info["partitions"].items():
        path = images / f"{source}.img" if source else None
        length = path.stat().st_size if path else 0
        args.extend(["--partition", f"{n}:{attr}:{length}:{group}"])
        if path and sparse and not metadata_only:
            args.extend(["--image", f"{n}={path}"])
    run(*args)
    if not sparse and not metadata_only:
        fill_raw_extents(info, images, output)


def fill_raw_extents(info, images, output):
    """Stream raw image data into liblp's allocated extents, including >4 GiB inputs."""
    if output.stat().st_size != info["size"]:
        raise ValueError("Wrong size for liblp's full super image")
    dump = run("lpdump", output, capture_output=True, text=True).stdout
    table = dump.split("Super partition layout:", 1)[0]
    sections = dict(re.findall(r"^  Name: (\S+)\n(.*?)(?=^-{4,})", table, re.M | re.S))
    if sections.keys() != info["partitions"].keys():
        raise ValueError("Unexpected partitions in generated super metadata")
    first = int(re.search(r"^  First sector: (\d+)$", dump, re.M)[1])
    with output.open("r+b") as dest:
        for part, (_, _, source) in info["partitions"].items():
            if not source:
                continue
            ranges = re.findall(r"^    (\d+) \.\. (\d+) linear super (\d+)$", sections[part], re.M)
            with (images / f"{source}.img").open("rb") as src:
                size = (images / f"{source}.img").stat().st_size
                for start, end, sector in ranges:
                    start, end, sector = map(int, (start, end, sector))
                    count = (end - start + 1) * 512
                    if (start * 512 != src.tell() or count <= 0 or count > size - src.tell()
                            or sector < first or sector * 512 + count > info["size"]):
                        raise ValueError(f"Invalid linear extent for {part}")
                    dest.seek(sector * 512)
                    while count:
                        data = src.read(min(count, 4 << 20))
                        if not data:
                            raise ValueError(f"Truncated image for {part}")
                        dest.write(data)
                        count -= len(data)
                if src.tell() != size:
                    raise ValueError(f"Incomplete extent map for {part}")


def grow_xiaomi_ext4(info, images, targets, work):
    # Model the minimum layout using liblp itself, including geometry and alignment.
    # Lift the limits only for this metadata-only measurement so deficits are measurable.
    parts = info["partitions"]
    sizes = {n: (images / f"{src}.img").stat().st_size if src else 0
             for n, (_, _, src) in parts.items()}
    unit = info["alignment"]  # Xiaomi OTA layouts use 1 MiB alignment and 4 KiB ext4 blocks.
    probe = work / "super-minimum.lp"
    geometry = dict(info, size=info["size"] + sum(sizes.values()) + len(parts) * unit,
                    groups=dict.fromkeys(info["groups"], 0))
    write_super(geometry, images, probe, sparse=False, metadata_only=True)
    dump = run("lpdump", probe, capture_output=True, text=True).stdout
    end_sectors = re.findall(r"^super: \d+ \.\. (\d+):", dump, re.M)
    if not end_sectors:
        raise ValueError("Missing physical extents in super layout")
    free = info["size"] - max(map(int, end_sectors)) * 512
    if free < 0:
        raise ValueError(f"Minimum partition layout exceeds physical super by {-free} bytes")

    budgets, weights = {}, {}
    for group, limit in info["groups"].items():
        members = [n for n in sizes if parts[n][0] == group]
        used = sum(sizes[n] for n in members)
        if limit and used > limit:
            raise ValueError(f"Minimum layout exceeds {group} by {used - limit} bytes")
        weight = sum(sizes[n] for n in targets if n in members)
        if weight:
            budgets[group] = (min(free, limit - used) if limit else free) // unit
            weights[group] = weight

    # Allocate the most tightly capped groups first, then distribute their unused share.
    chunks = min(free // unit, sum(budgets.values()))
    weight_left = sum(weights.values())
    for group in sorted(weights, key=lambda g: budgets[g] / weights[g]):
        share = min(budgets[group], chunks * weights[group] // weight_left)
        chunks -= share
        weight_left -= weights[group]
        remaining = weights[group]
        for n in (n for n in targets if parts[n][0] == group):
            growth = share * sizes[n] // remaining
            remaining -= sizes[n]
            share -= growth
            size = sizes[n] + growth * unit
            path = images / f"{parts[n][2]}.img"
            if growth:
                run("resize2fs", path, str(size // 4096))
                run("e2fsck", "-fn", path)
            if path.stat().st_size != size:
                raise ValueError(f"Unexpected ext4 size for {n}")
            print(f"{n}: minimum {sizes[n]} bytes, added {growth * unit}, final {size}", flush=True)


def repack(info, images, output, sparse):
    write_super(info, images, output, sparse)
    if sparse:
        with output.open("rb") as f:
            magic, major, _, _, _, block, count, _, _ = struct.unpack("<I4H4I", f.read(28))
        if magic != 0xed26ff3a or major != 1 or block * count != info["size"]:
            raise ValueError("Odin sparse super capacity mismatch")
    elif output.stat().st_size != info["size"]:
        raise ValueError("Raw super capacity mismatch")
