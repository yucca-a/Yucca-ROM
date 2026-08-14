#!/usr/bin/env python3
"""Fetch Xiaomi Stable/Beta ROM metadata from HyperOS.fans HyperData."""

import argparse
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATA_SOURCES = (
    ("HyperData", "https://data.hyperos.fans/devices/{codename}.json"),
    ("HyperData GitHub", "https://raw.githubusercontent.com/HegeKen/HyperData/main/devices/{codename}.json"),
    ("HyperOS.fans snapshot", "https://hyperos.fans/data/devices/{codename}.json"),
)
DOWNLOAD_MIRRORS = (
    "https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/{version}/{filename}",
    "https://cdnorg.d.miui.com/{version}/{filename}",
    "https://bn.d.miui.com/{version}/{filename}",
)
REGION_MAP = {
    "CN": "cn",
    "GL": "global",
    "EEA": "eea",
    "IN": "in",
    "RU": "ru",
    "TW": "tw",
    "ID": "id",
    "LM": "lm",
    "JP": "global",
    "KR": "global",
    "TR": "eea",
}
EXCLUDE_KEYWORDS = ("demo", "演示", "政企", "enterprise", "claro", "运营商定制")
BETA_KEYWORDS = ("beta", "测试版")
NON_STABLE_KEYWORDS = BETA_KEYWORDS + ("preview", "开发者预览", "开发版")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Yucca-ROM/1.0"


def branch_names(branch: dict) -> str:
    return " ".join(str(value) for value in (branch.get("name") or {}).values()).lower()


def fetch_device(codename: str) -> tuple[dict, str]:
    errors = []
    for source_name, source_url in DATA_SOURCES:
        url = source_url.format(codename=codename)
        request = Request(
            url,
            headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("branches"), list):
                raise ValueError("响应缺少 branches")
            return data, source_name
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as error:
            errors.append(f"{source_name}: {error}")
    raise RuntimeError("；".join(errors))


def pick_branches(device: dict, region: str, channel: str) -> list[dict]:
    target_region = REGION_MAP.get(region.upper(), region.lower())
    matches = []
    for branch in device.get("branches", []):
        if not isinstance(branch, dict) or branch.get("region") != target_region:
            continue
        names = branch_names(branch)
        if any(keyword in names for keyword in EXCLUDE_KEYWORDS):
            continue
        if channel == "beta":
            if any(keyword in names for keyword in BETA_KEYWORDS):
                matches.append(branch)
        elif not any(keyword in names for keyword in NON_STABLE_KEYWORDS):
            score = 1 if "stable" in names or "正式版" in names else 0
            matches.append((score, branch))

    if channel == "stable":
        matches.sort(key=lambda item: item[0], reverse=True)
        return [branch for _, branch in matches]
    return matches


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def pick_rom(branches: list[dict], version_pin: str | None) -> tuple[str, str, dict] | None:
    if version_pin and version_pin.upper() not in ("LATEST",) and "AUTO" not in version_pin.upper():
        for branch in branches:
            rom = (branch.get("roms") or {}).get(version_pin)
            if isinstance(rom, dict):
                filename = (rom.get("recovery") or rom.get("fastboot") or "").strip()
                if filename:
                    return version_pin, filename, branch
        return None

    candidates = []
    for branch in branches:
        for version, rom in (branch.get("roms") or {}).items():
            if not isinstance(rom, dict):
                continue
            filename = (rom.get("recovery") or rom.get("fastboot") or "").strip()
            if filename:
                candidates.append((rom.get("release", ""), version_key(version), version, filename, branch))
    if not candidates:
        return None
    _, _, version, filename, branch = max(candidates, key=lambda item: (item[0], item[1]))
    return version, filename, branch


def main() -> None:
    parser = argparse.ArgumentParser(description="从 HyperData 获取 Xiaomi ROM")
    parser.add_argument("--codename", required=True)
    parser.add_argument("--region", default="CN")
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("--version", default=None, help="精确版本；AUTO/LATEST 表示自动选择最新")
    args = parser.parse_args()

    try:
        device, source = fetch_device(args.codename)
        branches = pick_branches(device, args.region, args.channel)
        if not branches:
            raise ValueError(f"region={args.region} 下未找到 {args.channel} 分支")
        chosen = pick_rom(branches, args.version if args.channel == "stable" else None)
        if not chosen:
            detail = f"版本 {args.version}" if args.version else "可下载版本"
            raise ValueError(f"{args.channel} 分支未找到{detail}")
    except (RuntimeError, ValueError) as error:
        print(f"[!] ROM 获取失败: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    version, filename, branch = chosen
    if filename.startswith(("http://", "https://")):
        urls = [filename]
        filename = filename.rsplit("/", 1)[-1].split("?", 1)[0]
    else:
        urls = [mirror.format(version=version, filename=filename) for mirror in DOWNLOAD_MIRRORS]

    print(urls[0])
    print(version)
    print(filename)
    print(" ".join(urls))

    print(f"[*] 数据源: {source}", file=sys.stderr)
    print(f"[*] 通道: {args.channel}", file=sys.stderr)
    print(f"[*] 分支: {branch_names(branch)}", file=sys.stderr)
    print(f"[*] 版本: {version}", file=sys.stderr)
    print(f"[*] 文件: {filename}", file=sys.stderr)
    print(f"[*] 镜像: {len(urls)} 个", file=sys.stderr)


if __name__ == "__main__":
    main()
