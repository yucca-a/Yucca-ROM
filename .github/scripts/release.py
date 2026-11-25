"""Stage one ROM and append its verified assets to a model-specific GitHub Release."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from random import uniform
from urllib.parse import quote


ASSET_LIMIT = 2 * 1024**3  # GitHub requires each asset to be strictly smaller.
PART_SIZE = 1900 * 1024**2
BUFFER = 8 * 1024**2
INDEX = "<!-- rom-builds -->"
ENCRYPTION_LABELS = {"encrypted": "加密Data", "decrypted": "解密Data"}


def identity(info):
    for key in ("device", "version", "channel", "build"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", info[key]):
            raise ValueError(f"Invalid {key}: {info[key]!r}")
    model = info["model"]
    if not model or len(model.splitlines()) != 1:
        raise ValueError("Expected a single-line device name")
    device = info["device"]
    title = f"{model} ({device})"
    # Git forbids ASCII spaces in refs. Non-breaking spaces retain the model's typography.
    tag = title.replace(" ", "\u00a0")
    subprocess.run(["git", "check-ref-format", f"refs/tags/{tag}"], check=True)
    variant = info.get("encryption")
    if variant is not None and variant not in ENCRYPTION_LABELS:
        raise ValueError(f"Invalid encryption variant: {variant!r}")
    suffix = variant or f"r{info['build']}"  # Legacy builds without a recorded variant.
    archive = f"YuccaROM_{device}_{info['version']}_{info['channel']}_{suffix}.zip"
    return tag, title, archive


def stage(folder):
    env = os.environ
    if env["CODENAME"] != env["DEVICE"]:
        raise ValueError("Device profile does not match the build matrix")
    mods = []
    if env["BRAND"].lower() == "xiaomi":
        fs = env.get("SYSTEM_FORMAT", "Official").lower()
        label = {"official": "保留官方格式", "profile": "保留官方格式", "ext4": "可写 ext4", "f2fs": "可写 F2FS"}[fs]
        mods.append(f"system / product / system_ext {label}")
    for flag, skip, label in (
        ("KSU_PATCH", "SKIP_KSU", "KernelSU"), ("DEBLOAT", "SKIP_DEBLOAT", "精简"),
        ("REMOVE_AVB", "", "去 AVB"),
    ):
        if env.get(flag, "true") == "true" and env.get(skip) != "true":
            mods.append(label)
    remove_encryption = env.get("REMOVE_ENCRYPT", "true").lower() == "true" and env.get("KEEP_ENCRYPTION") != "true"
    info = dict(device=env["DEVICE"], model=env["DEVICE_NAME"].strip(), version=env["VERSION"],
                encryption="decrypted" if remove_encryption else "encrypted",
                channel=env["CHANNEL"], brand=env["BRAND"].lower(), mods=mods,
                build=f"{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}")
    if info["brand"] == "xiaomi":
        match = re.fullmatch(r"(OS[0-9]+(?:\.[0-9]+)+)\.[A-Z]{7}", info["version"])
        if match:
            info["stock_version"] = info["version"]
            info["version"] = match[1]
    _, _, name = identity(info)
    archive = Path(env["ARCHIVE"])
    if not archive.is_file() or not archive.stat().st_size:
        raise ValueError("Missing or empty ROM")
    with archive.open("rb") as source:
        info["sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
    folder.mkdir()
    shutil.move(archive, folder / name)
    (folder / "build.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def part_name(archive, index):
    return f"{archive.stem}-{index}.zip"


def assets(archive, temp, limit=ASSET_LIMIT, part_size=PART_SIZE):
    """Yield one upload at a time. Only the current part needs extra disk space."""
    size = archive.stat().st_size
    if not size:
        raise ValueError("Empty ROM")
    whole = hashlib.sha256()
    with archive.open("rb") as source:
        if size < limit:
            while block := source.read(BUFFER):
                whole.update(block)
            yield archive, whole.hexdigest()
        else:
            for index, offset in enumerate(range(0, size, part_size), start=1):
                part = temp / part_name(archive, index)
                digest = hashlib.sha256()
                remaining = min(part_size, size - offset)
                with part.open("wb") as output:
                    while remaining:
                        block = source.read(min(BUFFER, remaining))
                        if not block:
                            raise ValueError("Truncated ROM")
                        whole.update(block)
                        digest.update(block)
                        output.write(block)
                        remaining -= len(block)
                yield part, digest.hexdigest()
                part.unlink()
    checksum = temp / (archive.name + ".sha256")
    checksum.write_bytes(f"{whole.hexdigest()}  {archive.name}\n".encode("ascii"))
    yield checksum, hashlib.sha256(checksum.read_bytes()).hexdigest()


def gh(*args, payload=None):
    result = subprocess.run(["gh", *args], input=payload, capture_output=True, encoding="utf-8")
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        if re.search(r"HTTP (?:408|429|5\d\d)|unexpected (?:end of JSON input|EOF)|"
                     r"timed? ?out|connection (?:reset|refused)|TLS handshake timeout|"
                     r"secondary rate limit|already_exists", message, re.I):
            raise ConnectionError(message)
        raise RuntimeError(message or f"gh exited with status {result.returncode}")
    return result.stdout


def retry_remote(label, operation):
    for attempt in range(6):
        try:
            return operation()
        except (ConnectionError, json.JSONDecodeError) as exc:
            if attempt == 5:
                raise
            delay = min(10 * 2**attempt, 90) + uniform(0, 5)
            print(f"{label}: {exc}. Retrying in {delay:.0f}s", file=sys.stderr, flush=True)
            time.sleep(delay)


def request(method, endpoint, **fields):
    return json.loads(gh("api", "-X", method, endpoint, "--input", "-", payload=json.dumps(fields)))


def list_entries(endpoint, query):
    lines = gh("api", "--paginate", endpoint + "?per_page=100", "--jq", query)
    return [json.loads(line) for line in lines.splitlines() if line]


def notes_for(info):
    if info["brand"] == "xiaomi":
        flashing = Path(".github/release-templates/xiaomi.md").read_text(encoding="utf-8")
    else:
        flashing = "### 刷入方式\n\n解压 ZIP 后，用 Odin 将 AP 和 CSC 两个 `.tar` 文件填入对应槽位刷入。刷机前请备份数据。"
    return ("## ROM 下载\n\n选择固件版本和 Data 版本，同一行的附件属于同一个刷机包。\n\n"
            + INDEX + "\n\n### 合并与校验\n\n"
            "单个 ZIP 可直接校验。分卷请全部下载到同一目录，按编号从小到大合并为整包。"
            "以两卷为例，将「包名」换成实际文件名前缀，更多分卷按顺序追加到输入文件列表。\n\n"
            "- Windows（cmd）：`copy /b 包名-1.zip+包名-2.zip 包名.zip`\n"
            "- Linux / macOS：`cat 包名-1.zip 包名-2.zip > 包名.zip`\n\n"
            "刷入前对照 `.sha256` 核对整包 SHA-256。\n\n"
            + flashing)


def add_entry(body, info, links, archive_name):
    marker = f"<!-- rom:{archive_name} -->"
    if marker in body:
        return body
    if INDEX not in body:
        raise ValueError("Device release is missing its ROM download index")
    mods = " · ".join(info["mods"]) or "默认"
    variant = info.get("encryption")
    label = f" · **{ENCRYPTION_LABELS[variant]}**" if variant else ""
    entry = (f"{marker}\n- **{info['version']}**{label} · {info['channel']}"
             f" · {mods} · " + " / ".join(links))
    return body.replace(INDEX, INDEX + "\n\n" + entry, 1)


def publish(folder):
    info = json.loads((folder / "build.json").read_text(encoding="utf-8"))
    if info["device"] != os.environ["DEVICE"]:
        raise ValueError("Downloaded artifact belongs to a different device")
    tag, title, name = identity(info)
    archive = folder / name
    if not archive.is_file() and info.get("encryption"):
        legacy = folder / f"{archive.stem}_r{info['build']}.zip"
        if legacy.is_file():
            legacy.rename(archive)
    size = archive.stat().st_size
    if not size:
        raise ValueError("Empty ROM")
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != info["sha256"]:
            raise ValueError("ROM differs from the build artifact")
    count = 1 if size < ASSET_LIMIT else (size + PART_SIZE - 1) // PART_SIZE
    names = ([name] if count == 1 else [part_name(archive, i) for i in range(1, count + 1)])
    names.append(name + ".sha256")
    repo = os.environ["GH_REPO"]
    endpoint = f"repos/{repo}/releases"
    def find_or_create():
        matches = list_entries(endpoint, f'.[] | select(.tag_name == {json.dumps(tag)}) | {{id,body,draft,immutable}}')
        return matches[0] if matches else request(
            "POST", endpoint, tag_name=tag, target_commitish=os.environ["GITHUB_SHA"],
            name=title, draft=True, body=notes_for(info))

    release = retry_remote(f"Find or create {title}", find_or_create)
    if release.get("immutable"):
        raise ValueError(f"Cannot append ROMs to immutable release {tag}")
    release_url = f"{endpoint}/{release['id']}"
    def remote_assets():
        rows = list_entries(release_url + "/assets", ".[] | {id,name,size,state,digest}")
        return {asset["name"]: asset for asset in rows}

    existing = retry_remote("List release assets", remote_assets)
    family = re.compile(re.escape(archive.stem) + r"(?:-[1-9][0-9]*)?\.zip")
    if any(family.fullmatch(asset) and asset not in names for asset in existing):
        raise ValueError(f"Conflicting release asset: {name} already has a different set of volumes")
    checksum = existing.get(names[-1])
    if checksum and checksum["state"] == "uploaded":
        manifest = f"{info['sha256']}  {name}\n".encode("ascii")
        if checksum["digest"] != "sha256:" + hashlib.sha256(manifest).hexdigest():
            raise ValueError(f"Conflicting release asset: {name} is already published with different content")
    if len(existing.keys() | set(names)) > 1000:
        raise ValueError(f"{tag} would exceed GitHub's limit of 1000 assets per release")
    encoded_tag = quote(tag, safe="")
    base = f"https://github.com/{repo}/releases/download/{encoded_tag}/"
    labels = (["ZIP"] if count == 1 else [f"分卷 {i}" for i in range(1, count + 1)]) + ["SHA-256"]
    links = [f"[{label}]({base}{filename})" for label, filename in zip(labels, names)]
    body = add_entry(release["body"], info, links, name)
    expected = {}
    with tempfile.TemporaryDirectory(dir=folder.parent) as temp:
        for path, digest in assets(archive, Path(temp), ASSET_LIMIT, PART_SIZE):
            expected[path.name] = "sha256:" + digest
            def ensure_uploaded():
                prior = remote_assets().get(path.name)
                if prior and prior["state"] == "starter":
                    gh("api", "-X", "DELETE", f"{endpoint}/assets/{prior['id']}")
                elif prior:
                    if (prior["state"] != "uploaded" or prior["size"] != path.stat().st_size
                            or prior["digest"] != "sha256:" + digest):
                        raise ValueError(f"Conflicting release asset: {path.name}")
                    return
                gh("release", "upload", tag, str(path), "--repo", repo)
            retry_remote(path.name, ensure_uploaded)

    def verify_uploads():
        uploaded = remote_assets()
        for filename, wanted in expected.items():
            asset = uploaded.get(filename, {})
            if asset.get("digest") and asset["digest"] != wanted:
                raise ValueError(f"Conflicting release asset: {filename}")
            if asset.get("state") != "uploaded" or asset.get("digest") != wanted:
                raise ConnectionError(f"GitHub has not confirmed the uploaded content of {filename}")
    retry_remote("Verify release assets", verify_uploads)
    if release["draft"] or body != release["body"]:
        retry_remote(f"Publish {title}", lambda: request("PATCH", release_url, body=body, draft=False))
    print(f"Published {title}: https://github.com/{repo}/releases/tag/{encoded_tag}")


if __name__ == "__main__":
    {"stage": stage, "publish": publish}[sys.argv[1]](Path(os.environ["RUNNER_TEMP"]) / "rom-release")
