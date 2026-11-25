#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
brand=xiaomi
device=
stock=
out=/tmp/yucca_local_out
profiles=${ROM_PROFILES:-"$root/YAK-Profiles"}
tools=${ROM_TOOLS:-"$root/.rom-tools"}
provider=none
project=local_build
extra=()
fetch_apks=auto
keep_encryption=false
system_format=official
gpi_only=false
while (($#)); do
    case "$1" in
        --device) device=$2; shift 2 ;;
        --zip) stock=$2; shift 2 ;;
        --out) out=$2; shift 2 ;;
        --brand) brand=$2; shift 2 ;;
        --profiles) profiles=$2; shift 2 ;;
        --tools) tools=$2; shift 2 ;;
        --upload) provider=$2; shift 2 ;;
        --project) project=$2; shift 2 ;;
        --keep-encryption) keep_encryption=true; shift ;;
        --system-format) system_format=$2; shift 2 ;;
        --skip-apks) fetch_apks=false; shift ;;
        --inject-apks) fetch_apks=true; shift ;;
        --gpi-out) gpi_only=true; extra+=("$1" "$2"); shift 2 ;;
        --version) extra+=("$1" "$2"); shift 2 ;;
        --skip-ksu|--skip-debloat|--swap-kernel) extra+=("$1"); shift ;;
        -h|--help) echo 'local_build.sh --device CODENAME --zip STOCK.zip [--brand xiaomi|Samsung] [--profiles DIR] [--tools DIR] [--skip-ksu] [--inject-apks|--skip-apks] [--skip-debloat] [--keep-encryption] [--system-format Official|Ext4|F2FS] [--swap-kernel] [--out DIR] [--project NAME] [--upload none|gofile|pixeldrain]'; exit ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ $brand != samsung ]] || brand=Samsung
[[ $brand == xiaomi || $brand == Samsung ]] && [[ $device =~ ^[A-Za-z0-9_-]+$ && $project =~ ^[A-Za-z0-9_-]+$ && -f $stock ]] || { echo 'Check --device, --brand, --project and --zip' >&2; exit 2; }
case "$provider" in none|gofile|pixeldrain) ;; *) echo "Invalid upload provider" >&2; exit 2 ;; esac
profiles=$(realpath "$profiles")
[[ -f "$profiles/$brand/CONFIGS/$device.conf" ]] || { echo "Missing $brand/$device profile in $profiles" >&2; exit 2; }
tools=$(realpath "$tools")
tool_brand=$brand
$gpi_only && tool_brand=tgy
if [[ ! -f "$tools/.ready-$tool_brand-$(sha256sum "$root/builder/setup.sh" | cut -c1-12)" ]]; then
    bash "$root/builder/setup.sh" "$tools" "$tool_brand"
    touch "$tools/.ready-$tool_brand-$(sha256sum "$root/builder/setup.sh" | cut -c1-12)"
fi
override_size=${ROM_SUPER_SIZE:-}
set -a
source "$profiles/$brand/CONFIGS/$device.conf"
set +a
if [[ $fetch_apks == auto ]]; then fetch_apks=${INJECT_APKS_DEFAULT:-true}; fi
if [[ $fetch_apks == false ]]; then extra+=(--skip-apks); fi
system_format=${system_format,,}
case "$brand/$system_format" in
    Samsung/official|Samsung/profile) ;;
    xiaomi/official|xiaomi/profile)
        # No override: the image editor detects the filesystem of each stock partition.
        unset SYSTEM{,_A,_B}_FS PRODUCT{,_A,_B}_FS SYSTEM_EXT{,_A,_B}_FS ;;
    xiaomi/ext4|xiaomi/f2fs)
        export SYSTEM_A_FS="$system_format" PRODUCT_A_FS="$system_format" SYSTEM_EXT_A_FS="$system_format" ;;
    *) echo "Unsupported --system-format for $brand: $system_format" >&2; exit 2 ;;
esac
if [[ " ${SYSTEM_A_FS:-} ${PRODUCT_A_FS:-} ${SYSTEM_EXT_A_FS:-} " == *" f2fs "* ]]; then
    if ! command -v guestfish >/dev/null || ! command -v mkfs.f2fs >/dev/null; then
        sudo apt-get update -qq
        sudo apt-get install -y --no-install-recommends guestfish f2fs-tools
    fi
fi
if [[ -n $override_size ]]; then export SUPER_DEVICE_SIZE=$override_size; fi
if $fetch_apks && ! $gpi_only; then
    python3 "$profiles/scripts/fetch_apks.py" --brand "$brand" --root "$profiles"
fi
build_env=()
while IFS= read -r var; do
    case "$var" in
        *_FS|*_EROFS_COMPRESSION|SUPER_DEVICE_SIZE|FIX_SCRIPTS|APPLY_EXTRA_PROPS|DEBLOAT|REMOVE_AVB|REMOVE_ENCRYPT|KSU_PATCH|KSU_INPUT|KSU_KMI|MODEL|SERIES|YUCCA_GPI_BUNDLE_DIR) build_env+=("$var=${!var}") ;;
    esac
done < <(compgen -e)
if $keep_encryption; then build_env+=("REMOVE_ENCRYPT=false"); fi
# The Samsung kernel hook uses the public-release API token when available.
[[ -z ${GITHUB_TOKEN:-} ]] || build_env+=("GITHUB_TOKEN=$GITHUB_TOKEN")
temp=$(mktemp -d -p "${ROM_WORK_DIR:-/var/tmp}" "yucca-$project-XXXXXXXX")
trap 'sudo rm -rf -- "$temp"' EXIT
if ! $gpi_only; then mkdir -p "$out"; fi
sudo env "${build_env[@]}" "PATH=$tools:/usr/sbin:/usr/bin:/sbin:/bin" "PYTHONPATH=$tools" \
    "PYTHONDONTWRITEBYTECODE=1" "ROM_WORK_DIR=$temp" unshare --mount --propagation private \
    python3 "$root/builder/build.py" --brand "$brand" --device "$device" --zip "$stock" \
    --profiles "$profiles" --tools "$tools" --out "$out" --result "$temp/result" "${extra[@]}"
if ! $gpi_only; then
    archive=$(cat "$temp/result")
    if [[ -n ${GITHUB_OUTPUT:-} ]]; then
        printf 'archive=%s\narchive_name=%s\n' "$archive" "$(basename "$archive")" >> "$GITHUB_OUTPUT"
    fi
    if [[ $provider != none ]]; then
        bash "$profiles/scripts/upload.sh" "$archive" "$provider"
    fi
fi
