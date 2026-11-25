#!/usr/bin/env bash
# Install into a dedicated directory; every external binary/archive has a pinned digest.
set -euo pipefail
dest=$(realpath -m "${1:?usage: setup.sh TOOL_DIRECTORY [xiaomi|Samsung|tgy]}")
brand=${2:-xiaomi}
[[ $(uname -sm) == 'Linux x86_64' ]] || { echo "Requires x86_64 Linux" >&2; exit 1; }
sudo apt-get update -qq
packages=(python3-venv python3-protobuf protobuf-compiler python3-libarchive-c erofs-utils
          e2fsprogs android-sdk-libsparse-utils lz4 zstd selinux-utils bubblewrap aria2 unzip zip curl)
[[ $brand != xiaomi ]] || packages+=(gcc-aarch64-linux-gnu make)
sudo apt-get install -y "${packages[@]}"
mkdir -p "$dest"
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
fetch() {
    curl --fail --location --retry 4 --silent --show-error "$1" -o "$tmp/$2"
    echo "$3  $tmp/$2" | sha256sum --check --status || { echo "Digest mismatch: $2" >&2; exit 1; }
}
base=https://raw.githubusercontent.com/itsNileshHere/android-lptools/b7b5c09916ab88074692006db7dab34256da6e24/bin
fetch "$base/lpmake" lpmake bca9880b1ab76a5718e885158658a968d49c34c33d2242aadec870207bd581d3
fetch "$base/lpdump" lpdump da0eeb6e3cafc471df3199f681fb9d7d0ad5421763101baa05bb149ead7a7a4b
fetch "$base/lpunpack" lpunpack 3c01cd0c4a5bb5c596617e236951c004bbacb91da7d8b33d9bf4d78e4dd1b0b5
install "$tmp"/lp* "$dest/"
if [[ $brand == xiaomi ]]; then
    base=https://github.com/ssut/payload-dumper-go/releases/download/2.1.0
    fetch "$base/payload-dumper-go_2.1.0_linux_amd64.tar.gz" payload.tgz bbbb53a71955c69272afdef7bc7e83a1bb1770f453d0c21dcaf61a3ba0463c11
    tar -xzf "$tmp/payload.tgz" -C "$tmp" payload-dumper-go
    install "$tmp/payload-dumper-go" "$dest/"
    fetch https://raw.githubusercontent.com/ssut/payload-dumper-go/c3a50ba8d784764c06143c1f82971cd81a77f9d6/update_metadata.proto update_metadata.proto 11b94f469c3ab3e7093ab26e39277d481d19351bce3e430553e3ffb44a263f05
    protoc -I "$tmp" --python_out="$dest" "$tmp/update_metadata.proto"
    base=https://github.com/facebook/zstd/releases/download/v1.5.7
    fetch "$base/zstd-1.5.7.tar.gz" zstd.tgz eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3
    tar -xzf "$tmp/zstd.tgz" -C "$tmp"
    make -j"$(nproc)" -C "$tmp/zstd-1.5.7/programs" zstd CC=aarch64-linux-gnu-gcc LDFLAGS=-static
    install "$tmp/zstd-1.5.7/programs/zstd" "$dest/zstd_arm64"
    fetch "$base/zstd-v1.5.7-win64.zip" zstd.zip acb4e8111511749dc7a3ebedca9b04190e37a17afeb73f55d4425dbf0b90fad9
    unzip -joq "$tmp/zstd.zip" '*/zstd.exe' -d "$dest"
    fetch https://dl.google.com/android/repository/platform-tools_r36.0.2-win.zip platform.zip b024d4f319d6ad3004de1ba7b96a5c7c5f3512e8b14126308d598b4ab93dcead
    unzip -joq "$tmp/platform.zip" '*/adb.exe' '*/fastboot.exe' '*/AdbWinApi.dll' '*/AdbWinUsbApi.dll' -d "$dest"
else
    python3 -m venv --system-site-packages "$dest/venv"
    "$dest/venv/bin/pip" install pycryptodome==3.23.0
fi
[[ $brand != tgy ]] || exit 0
fetch https://raw.githubusercontent.com/LineageOS/android_external_avb/1119a81aa94fc4f6d9fc22ec19cb58808f9a63f2/avbtool.py avbtool fc5332131a907ae22978e16a84fa12a14d277a60d8e2ad77c31a0097dafeb657
fetch https://github.com/topjohnwu/Magisk/releases/download/v31.1/Magisk-v31.1.apk magisk.apk 89cefac0a318afa39fb3880c45c87b036c05bc1ebf02a7a715b7f8d7c26ac6e7
unzip -p "$tmp/magisk.apk" lib/x86_64/libmagiskboot.so > "$tmp/magiskboot"
base=https://github.com/tiann/KernelSU/releases/download/v3.3.1
fetch "$base/ksud-x86_64-unknown-linux-musl" ksud 29b50f946a3cc40b04aef3dd587adad227fe0f70edf9c56b02c84479f0b0e954
install "$tmp"/{ksud,magiskboot,avbtool} "$dest/"
if [[ $brand == xiaomi ]]; then
    fetch "$base/KernelSU_v3.3.1_32680-release.apk" KernelSU.apk b4a059ace71de2fa65cc7f12b1c58d55fc23ab25e057ee4230ce779742abe450
    install -m644 "$tmp/KernelSU.apk" "$dest/"
fi
