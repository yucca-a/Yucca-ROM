#!/usr/bin/env bash
set -euo pipefail
mkdir -p stock
source "YAK-Profiles/$BRAND/CONFIGS/$DEVICE.conf"
if [[ -n ${CUSTOM_URL:-} ]]; then
    [[ $CUSTOM_URL == https://* && $CUSTOM_URL != *[[:space:]]* ]] || { echo "Invalid firmware URL" >&2; exit 2; }
    urls=("$CUSTOM_URL")
    filename=$(basename "${CUSTOM_URL%%\?*}")
    version=custom
    [[ $filename =~ (OS[0-9.]+\.[A-Za-z0-9]+) ]] && version=${BASH_REMATCH[1]}
elif [[ $BRAND == xiaomi ]]; then
    python3 .github/scripts/fetch_xiaomi_rom.py --codename "$CODENAME" --region "$REGION" \
        --channel "$CHANNEL" --version "$SYSTEM_VERSION" > stock/source.txt
    mapfile -t source_info < stock/source.txt
    version=${source_info[1]}
    filename=${source_info[2]}
    read -ra urls <<< "${source_info[3]:-${source_info[0]}}"
else
    file=$(python3 YAK-Profiles/Samsung/scripts/fetch_samsung_firmware.py --model "$MODEL" \
        --region "${FIRMWARE_REGION:-$REGION}" --out-dir stock)
    filename=$(basename "$file")
    version=custom
fi
if [[ -v urls ]]; then
    [[ $filename == *.zip ]] || filename="stock-$DEVICE.zip"
    [[ $filename != */* && $filename != -* ]] || exit 2
    aria2c -x 12 -s 12 --max-tries=4 --allow-overwrite=false --auto-file-renaming=false \
        --console-log-level=warn -d stock -o "$filename" -- "${urls[@]}"
fi
if [[ $BRAND == Samsung ]]; then
    version=$(python3 - "stock/$filename" <<'PY'
import sys, zipfile
from pathlib import Path
with zipfile.ZipFile(sys.argv[1]) as z:
    ap, = [Path(n).name for n in z.namelist()
           if Path(n).name.startswith("AP_") and n.endswith((".tar", ".tar.md5")) and not n.startswith("__MACOSX/")]
    print(ap.split("_")[1])
PY
)
fi
test -s "stock/$filename"
printf 'file=%s\nversion=%s\n' "stock/$filename" "$version" >> "$GITHUB_OUTPUT"
