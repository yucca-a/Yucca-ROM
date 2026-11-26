#!/sbin/sh
# Recovery supplies the API version, UI file descriptor and ZIP path.
case ${2:-} in ''|*[!0-9]*) exit 1 ;; esac
[ -n "${3:-}" ] || exit 1
ui_fd="/proc/self/fd/$2"
archive=$3
work=$(mktemp -d /tmp/yuccarom.XXXXXX) || exit 1
log="$work/flash.log"

ui() {
    printf 'ui_print %s\nui_print\n' "$1" > "$ui_fd"
    printf '%s\n' "$1" >> "$log"
}

fail() {
    ui "Error: $1"
    exit 1
}

cleanup() {
    for storage in /sdcard /external_sd /storage/emulated/0; do
        if [ -w "$storage" ] && cp "$log" "$storage/YuccaROM_flash.log" 2>/dev/null; then
            printf 'ui_print Log: %s/YuccaROM_flash.log\n' "$storage" > "$ui_fd"
            break
        fi
    done
    rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

read_member() {
    case ${reader##*/} in
        python*) "$reader" -c '
import shutil, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    with archive.open(sys.argv[2]) as entry:
        shutil.copyfileobj(entry, getattr(sys.stdout, "buffer", sys.stdout), 1024 * 1024)
' "$archive" "$1" ;;
        7z|7za) "$reader" e -so "$archive" "$1" ;;
        *) "$reader" -p "$archive" "$1" ;;
    esac
}

read_payload() {
    if [ "$1" = super.img.zst ]; then
        dd if="$archive" bs=1048576 iflag=skip_bytes,count_bytes \
            skip="$super_offset" count="$compressed_bytes"
    else
        read_member "$1"
    fi
}

# Record each producer's result, including delayed ZIP CRC and zstd errors.
stream_member() {
    member=$1
    shift
    rm -f "$work/zip.status"
    (read_payload "$member" 2>>"$log"; echo "$?" > "$work/zip.status") | "$@" || return 1
    [ "$(cat "$work/zip.status" 2>/dev/null)" = 0 ]
}

decompress_super() {
    rm -f "$work/zstd.status"
    ("$zstd" -dc 2>>"$log"; echo "$?" > "$work/zstd.status") | "$@" || return 1
    [ "$(cat "$work/zstd.status" 2>/dev/null)" = 0 ]
}

check_length() {
    length=$(wc -c) || return 1
    [ "$length" -eq "$1" ]
}

find_block() {
    for directory in /dev/block/by-name /dev/block/bootdevice/by-name \
        /dev/block/platform/*/by-name /dev/block/platform/*/*/by-name; do
        if [ -b "$directory/$1" ]; then
            printf '%s\n' "$directory/$1"
            return 0
        fi
    done
    return 1
}

positive_integer() {
    case $1 in ''|*[!0-9]*) return 1 ;; esac
    [ "$1" -gt 0 ]
}

add_target() {
    block=$1
    [ -w "$block" ] || fail "Partition is not writable: $block"
    capacity=$(blockdev --getsize64 "$block" 2>>"$log") || fail "Cannot read partition size: $block"
    [ "$capacity" -ge "$bytes" ] || fail "Partition too small: $block"
    printf '%s %s\n' "$part" "$block" >> "$work/targets"
}

write_block() {
    dd of="$1" bs=1048576 2>>"$log"
}

ui "YuccaROM: checking package and device"
command -v blockdev >/dev/null 2>&1 || fail "Recovery does not provide blockdev."
manifest_ready=
for candidate in unzip /sbin/unzip /system/bin/unzip /usr/bin/unzip /bin/unzip 7z 7za python3 python; do
    reader=$(command -v "$candidate" 2>/dev/null) || continue
    if read_member META-INF/yucca/flash_manifest.txt > "$work/manifest" 2>>"$log" &&
        read_member META-INF/yucca/super_range.txt > "$work/super-range" 2>>"$log" &&
        [ -s "$work/manifest" ] && [ -s "$work/super-range" ]; then
        manifest_ready=1
        break
    fi
done
[ -n "$manifest_ready" ] || fail "Cannot extract the flash manifest."

{
    IFS= read -r version && IFS= read -r product && IFS= read -r super_bytes &&
        cat > "$work/firmware"
} < "$work/manifest" || fail "Incomplete flash manifest."
[ "$version" = YUCCA_FLASH_V1 ] || fail "Unsupported flash manifest version."
positive_integer "$super_bytes" || fail "Invalid super image size."
read -r super_offset compressed_bytes extra < "$work/super-range" || fail "Invalid super payload range."
archive_bytes=$(stat -c %s "$archive" 2>>"$log") || fail "Cannot read ZIP size."
if [ -n "$extra" ] || ! positive_integer "$super_offset" || ! positive_integer "$compressed_bytes" ||
    [ "$super_offset" -ge "$archive_bytes" ] || [ "$compressed_bytes" -gt "$((archive_bytes - super_offset))" ]; then
    fail "Super payload is outside the ZIP."
fi
case $product in ''|*[!A-Za-z0-9_.-]*) fail "Invalid product in flash manifest." ;; esac
if [ "$product" != "$(getprop ro.product.device)" ] &&
    [ "$product" != "$(getprop ro.build.product)" ] &&
    [ "$product" != "$(getprop ro.product.vendor.device)" ]; then
    fail "This package is for $product."
fi
[ "$(getprop ro.boot.flash.locked)" != 1 ] || fail "Bootloader is locked."
awk '$2 ~ /^\/(system|system_root|vendor|product|system_ext|odm|vendor_dlkm|system_dlkm|odm_dlkm)(\/|$)/ { exit 1 }' \
    /proc/mounts || fail "Unmount Android system partitions in Recovery before retrying."

can_select_a=
if command -v bootctl >/dev/null 2>&1; then
    case $(bootctl get-snapshot-merge-status 2>>"$log") in
        merging|snapshotted) fail "An OTA snapshot is pending. Complete the update before flashing." ;;
    esac
    if [ "$(bootctl get-suffix 0 2>>"$log")" = _a ]; then
        can_select_a=1
    fi
fi
case $(getprop ro.boot.slot_suffix) in
    _a) ;;
    _b) [ -n "$can_select_a" ] || fail "Recovery cannot switch from slot B to slot A. Use Fastboot." ;;
    *) fail "Unable to identify the current A/B slot." ;;
esac

super_block=$(find_block super) || fail "Missing super partition."
[ -w "$super_block" ] || fail "super is not writable."
super_capacity=$(blockdev --getsize64 "$super_block" 2>>"$log") || fail "Cannot read super partition size."
[ "$super_capacity" -eq "$super_bytes" ] || fail "super image size does not match the device."

: > "$work/targets"
: > "$work/images"
while read -r part bytes extra; do
    case $part in
        ''|*[!A-Za-z0-9_.-]*|super|userdata|metadata|frp|userdata_[ab]|metadata_[ab]|frp_[ab])
            fail "Invalid firmware image: $part" ;;
    esac
    if [ -n "$extra" ] || ! positive_integer "$bytes"; then
        fail "Invalid image size: $part"
    fi
    a=$(find_block "${part}_a")
    b=$(find_block "${part}_b")
    if [ -n "$a$b" ]; then
        [ -n "$a" ] && [ -n "$b" ] || fail "Missing A/B partition for $part."
        add_target "$a"
        add_target "$b"
    else
        single=$(find_block "$part") || fail "Missing partition: $part"
        add_target "$single"
    fi
    printf '%s %s\n' "$part" "$bytes" >> "$work/images"
done < "$work/firmware"
[ -s "$work/targets" ] || fail "No firmware images in flash manifest."

zstd=$(command -v zstd 2>/dev/null)
if [ -z "$zstd" ]; then
    zstd="$work/zstd"
    read_member META-INF/zstd_arm64 > "$zstd" 2>>"$log" || fail "Cannot extract bundled zstd."
    chmod 700 "$zstd" || fail "Cannot prepare bundled zstd."
fi
"$zstd" --version >>"$log" 2>&1 || fail "zstd cannot run in this Recovery."

ui "Checking the ZIP64 super stream and firmware images before writing"
stream_member super.img.zst decompress_super check_length "$super_bytes" ||
    fail "Cannot verify the super image. Check the ZIP or use Fastboot."
while read -r part bytes; do
    stream_member "images/$part.img" check_length "$bytes" || fail "Invalid ZIP member: $part.img"
done < "$work/images"

ui "Writing super"
stream_member super.img.zst decompress_super write_block "$super_block" || fail "Failed to write super."
while read -r part block; do
    ui "Writing ${block##*/}"
    stream_member "images/$part.img" write_block "$block" || fail "Failed to write ${block##*/}."
done < "$work/targets"
sync || fail "Unable to sync flashed partitions."
if [ -n "$can_select_a" ]; then
    bootctl set-active-boot-slot 0 >>"$log" 2>&1 || fail "Could not activate slot A."
fi
ui "Flash complete. Reboot to Android."
