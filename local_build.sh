#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# local_build.sh — 本地复现 CI 构建流程
#
# 用法:
#   bash local_build.sh --device popsicle --zip /path/to/rom.zip [选项]
#
# 选项:
#   --device <codename>       设备代号 (必填)
#   --zip    <path>           ROM ZIP 路径 (必填, 跳过下载步骤)
#   --brand  <brand>          品牌目录 (默认: xiaomi)
#   --skip-ksu                跳过 KernelSU 补丁
#   --upload <gofile|pixeldrain|none>  上传到云盘 (默认: none)
#   --out    <dir>            输出目录 (默认: /tmp/yucca_local_out)
#   --project <name>          YAKit 项目名 (默认: local_build)
#
# 环境变量 (上传时可选):
#   GOFILE_TOKEN
#   PIXELDRAIN_KEY
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── 路径 ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YAKIT_DIR="/home/yucca/work/YAKit"
PROFILES_DIR="/home/yucca/work/YAK-Profiles"

# ── 默认参数 ─────────────────────────────────────────────────────────────────
DEVICE=""
ROM_ZIP=""
BRAND="xiaomi"
SKIP_KSU=false
UPLOAD_PROVIDER="none"
OUT_DIR="/tmp/yucca_local_out"
PROJECT="local_build"

# ── 解析参数 ─────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --device)   DEVICE="$2";           shift 2 ;;
    --zip)      ROM_ZIP="$2";          shift 2 ;;
    --brand)    BRAND="$2";            shift 2 ;;
    --skip-ksu) SKIP_KSU=true;         shift   ;;
    --upload)   UPLOAD_PROVIDER="$2";  shift 2 ;;
    --out)      OUT_DIR="$2";          shift 2 ;;
    --project)  PROJECT="$2";          shift 2 ;;
    *) echo "[!] 未知参数: $1" >&2; exit 1 ;;
  esac
done

[[ -z "$DEVICE" ]] && { echo "[!] 必须指定 --device <codename>"; exit 1; }
[[ -z "$ROM_ZIP" ]] && { echo "[!] 必须指定 --zip <path>"; exit 1; }
[[ ! -f "$ROM_ZIP" ]] && { echo "[!] 文件不存在: $ROM_ZIP"; exit 1; }

ROM_ZIP="$(realpath "$ROM_ZIP")"

# ── 颜色输出 ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
step() { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "\033[0;33m[~]${NC} $*"; }
err()  { echo -e "${RED}[!]${NC} $*" >&2; }

# ── 步骤 0: 准备目录 ──────────────────────────────────────────────────────────
step "0. 准备目录"
PROJECT_DIR="$YAKIT_DIR/PROJECTS/$PROJECT"
BUILD_DIR="$PROJECT_DIR/Build"
EXTRACTED_DIR="$PROJECT_DIR/Extracted"
mkdir -p "$PROJECT_DIR"/{Config,ROM,Logs,_logical_temp,Build,Extracted}
ok "项目目录: $PROJECT_DIR"

# ── 步骤 1: 合并 YAK-Profiles ─────────────────────────────────────────────────
step "1. 合并 YAK-Profiles (brand=$BRAND)"
# 通用脚本
cp -r "$PROFILES_DIR/scripts/." "$YAKIT_DIR/scripts/"
# 品牌特定内容
cp -r "$PROFILES_DIR/$BRAND/CONFIGS/." "$YAKIT_DIR/CONFIGS/"
cp -r "$PROFILES_DIR/$BRAND/fixes/."   "$YAKIT_DIR/fixes/"
[[ -d "$PROFILES_DIR/$BRAND/scripts" ]] && \
  cp -r "$PROFILES_DIR/$BRAND/scripts/." "$YAKIT_DIR/scripts/"
[[ -d "$PROFILES_DIR/$BRAND/apks" ]] && \
  cp -r "$PROFILES_DIR/$BRAND/apks" "$YAKIT_DIR/apks"
ok "YAK-Profiles ($BRAND) 已合并到 YAKit"

# ── 步骤 2: 加载设备配置 ───────────────────────────────────────────────────────
step "2. 加载设备配置: $DEVICE"
CONF="$YAKIT_DIR/CONFIGS/${DEVICE}.conf"
[[ ! -f "$CONF" ]] && { err "配置文件不存在: $CONF"; exit 1; }
set -a; source "$CONF"; set +a

CODENAME="${CODENAME:-$DEVICE}"
DEVICE_NAME="${DEVICE_NAME:-$DEVICE}"
REGION="${REGION:-CN}"
REMOVE_AVB="${REMOVE_AVB:-true}"
REMOVE_ENCRYPT="${REMOVE_ENCRYPT:-true}"
KSU_PATCH="${KSU_PATCH:-true}"
KSU_INPUT="${KSU_INPUT:-init_boot}"
FIX_SCRIPTS="${FIX_SCRIPTS:-}"
DEBLOAT="${DEBLOAT:-true}"

echo "  设备: $DEVICE_NAME ($CODENAME)"
echo "  去AVB: $REMOVE_AVB | 解密: $REMOVE_ENCRYPT | KSU: $KSU_PATCH | 精简: $DEBLOAT"

# ── 步骤 3: 检查并安装依赖 ────────────────────────────────────────────────────
step "3. 检查依赖"
MISSING=()
for cmd in python3 img2simg simg2img lz4 zstd brotli e2fsck mkfs.ext4 p7zip aria2c; do
  command -v "$cmd" &>/dev/null || MISSING+=("$cmd")
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  warn "缺少工具: ${MISSING[*]}"
  echo "  正在安装..."
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    android-sdk-libsparse-utils \
    lz4 xz-utils zstd brotli \
    erofs-utils e2fsprogs p7zip-full aria2 \
    gcc-aarch64-linux-gnu
  pip install --break-system-packages pycryptodome 2>/dev/null || true
else
  ok "依赖已满足"
  # 确保交叉编译器已安装（arm64 zstd 内置到卡刷包用）
  if ! command -v aarch64-linux-gnu-gcc &>/dev/null; then
    echo "  安装 gcc-aarch64-linux-gnu..."
    sudo apt-get install -y -qq gcc-aarch64-linux-gnu
  fi
fi

python3 -c "import Crypto" 2>/dev/null || \
  pip install --break-system-packages pycryptodome 2>/dev/null || true

# ── 步骤 4: 初始化 YAKit .bin 环境 ────────────────────────────────────────────
step "4. YAKit .bin 环境"
_bin="$YAKIT_DIR/.bin"
_bin_ready() {
  [[ -f "$_bin/venv/bin/python3" ]] && \
  [[ -f "$_bin/venv/bin/payload_dumper" ]] && \
  [[ -f "$_bin/avbtool/avbtool.py" ]] && \
  [[ -f "$_bin/magiskboot" ]]
}
if _bin_ready; then
  ok ".bin 环境已就绪，跳过 full setup"
elif [[ -f "$_bin/setup-bin-env.sh" ]]; then
  sudo bash "$_bin/setup-bin-env.sh" --no-banner
  ok ".bin 环境就绪"
else
  warn ".bin/setup-bin-env.sh 不存在, 跳过"
fi

# ── 步骤 5: 复制 ROM ZIP 到 INPUT ─────────────────────────────────────────────
step "5. 准备 ROM 文件"
mkdir -p "$YAKIT_DIR/INPUT"
FILENAME="$(basename "$ROM_ZIP")"
VERSION=$(echo "$FILENAME" | grep -oP 'OS[\d.]+\.\w+' || echo "local")
# 本地直接链接 (避免复制大文件)
if [[ "$(realpath "$YAKIT_DIR/INPUT/$FILENAME" 2>/dev/null || true)" != "$ROM_ZIP" ]]; then
  ln -sf "$ROM_ZIP" "$YAKIT_DIR/INPUT/$FILENAME"
fi
ok "ROM: $FILENAME (version=$VERSION)"

# ── 步骤 6: 解包 ROM ──────────────────────────────────────────────────────────
step "6. 解包 ROM"
cd "$YAKIT_DIR"
sudo python3 yucca_android_kitchen.py --conf="\
  ACTION=extract_rom,\
  ZIP_ROM=$FILENAME,\
  EXTRACT_DIR=$EXTRACTED_DIR,\
  PROJECT_NAME=$PROJECT"
ok "解包完成"
ls "$PROJECT_DIR/ROM/" | head -10

# ── 步骤 7: 清理逻辑分区镜像释放空间 ──────────────────────────────────────────
step "7. 清理逻辑分区镜像"
# 只删除将被打包进 super.img 的逻辑分区，保留所有物理分区镜像
SUPER_LOGICAL=(system product vendor odm system_ext system_dlkm vendor_dlkm mi_ext odm_dlkm)
# 本地构建不删 INPUT 中的原始 ZIP（本地磁盘充裕，保留备用）
rm -f "$EXTRACTED_DIR/super.img"               # 原始 super.img（6GB+）
for lp in "${SUPER_LOGICAL[@]}"; do
  for suffix in "" _a _b; do
    rm -f "$EXTRACTED_DIR/${lp}${suffix}.img"
  done
  echo "  [-] ${lp}.img"
done
df -h /

# ── 步骤 8: 修复脚本 ──────────────────────────────────────────────────────────
step "8. 修复脚本"
ROM_DIR="$PROJECT_DIR/ROM"
if [[ -n "$FIX_SCRIPTS" ]]; then
  for script in $FIX_SCRIPTS; do
    SCRIPT_PATH="$YAKIT_DIR/fixes/$script"
    if [[ -f "$SCRIPT_PATH" ]]; then
      echo "  [*] $script"
      python3 "$SCRIPT_PATH" "$ROM_DIR"
    else
      warn "脚本不存在: $script"
    fi
  done
else
  warn "FIX_SCRIPTS 为空, 跳过"
fi

# ── 步骤 9: 精简 ──────────────────────────────────────────────────────────────
step "9. 精简 (Debloat)"
if [[ "$DEBLOAT" == "true" ]]; then
  python3 - <<PYEOF
import tomllib, shutil
from pathlib import Path

rom_dir = Path("$ROM_DIR")
config  = Path("$YAKIT_DIR/CONFIGS/tasks.toml")
if not config.is_file():
    print("[~] tasks.toml 不存在, 跳过精简")
    exit(0)

tasks = tomllib.loads(config.read_text())['tasks']
removed = 0
for t in tasks:
    if t.get('action') == 'remove':
        target = rom_dir / t['path']
        if target.exists():
            shutil.rmtree(target)
            print(f"  [-] {t['path']}")
            removed += 1
        else:
            print(f"  [~] Not found: {t['path']}")
print(f"[✓] 精简完成: {removed} 项已移除")
PYEOF
else
  warn "DEBLOAT=false, 跳过"
fi

# ── 步骤 10: Fstab 补丁 ────────────────────────────────────────────────────────
step "10. Fstab 补丁 (去AVB + 去加密)"
cd "$YAKIT_DIR"
sudo python3 yucca_android_kitchen.py --conf="\
  ACTION=fstab_patch,\
  ROM_DIR=PROJECTS/$PROJECT/ROM,\
  REMOVE_AVB=$REMOVE_AVB,\
  REMOVE_ENCRYPT=$REMOVE_ENCRYPT"

# ── 步骤 10.5: 重打包修改后的 vendor_boot ────────────────────────────────────
# vendor_boot 在 extract_rom 阶段被解包到 ROM/vendor_boot/ 用于 fstab 补丁,
# 这里必须 boot_repack 回 Extracted/vendor_boot.img, 否则后续步骤 17 收集到的
# 仍是原始未修改版本, 导致 fstab 补丁失效 (老 bug).
step "10.5. 重打包 vendor_boot"
VB_SRC="$PROJECT_DIR/ROM/vendor_boot"
VB_OUT="$EXTRACTED_DIR/vendor_boot.img"
if [[ -d "$VB_SRC" && -d "$VB_SRC/.repack_info" ]]; then
  cd "$YAKIT_DIR"
  sudo python3 yucca_android_kitchen.py --conf="\
    ACTION=boot_repack,\
    SOURCE_DIR=$VB_SRC,\
    OUTPUT_IMAGE=$VB_OUT"
  ok "vendor_boot.img 已用补丁后的 fstab 重新打包"
else
  warn "$VB_SRC 不存在或缺少 .repack_info/, 跳过 vendor_boot 重打包"
fi

# ── 步骤 11: Deodex ────────────────────────────────────────────────────────────
step "11. Deodex"
cd "$YAKIT_DIR"
sudo python3 yucca_android_kitchen.py --conf="\
  ACTION=deodex,\
  PROJECT_NAME=$PROJECT"

# ── 步骤 12: 追加 build.prop ───────────────────────────────────────────────────
step "12. Patch build.prop"
cd "$YAKIT_DIR"
bash scripts/patch_props.sh \
  "PROJECTS/$PROJECT/ROM" \
  "CONFIGS/extra_props.txt"

# ── 步骤 13: APK 注入 ─────────────────────────────────────────────────────────
step "13. 注入 APKs"
cd "$YAKIT_DIR"
if [[ -d "apks" ]]; then
  bash scripts/inject_apks.sh \
    "PROJECTS/$PROJECT/ROM" \
    "apks"
else
  warn "apks/ 目录不存在, 跳过"
fi

# ── 步骤 14: KernelSU 补丁 ────────────────────────────────────────────────────
step "14. KernelSU 补丁"
if [[ "$KSU_PATCH" == "true" && "$SKIP_KSU" == "false" ]]; then
  KSU_IMG="$EXTRACTED_DIR/${KSU_INPUT}.img"
  KSU_OUT="PROJECTS/$PROJECT/Build/${KSU_INPUT}.img"
  if [[ -f "$KSU_IMG" ]]; then
    cd "$YAKIT_DIR"
    sudo python3 yucca_android_kitchen.py --conf="\
      ACTION=ksu_patch,\
      INPUT_IMAGE=$KSU_IMG,\
      OUTPUT_IMAGE=$KSU_OUT"
    ok "KSU 补丁完成"
  else
    warn "$KSU_INPUT.img 不存在, 跳过 KSU"
  fi
elif [[ "$SKIP_KSU" == "true" ]]; then
  warn "已跳过 KSU (--skip-ksu)"
else
  warn "KSU_PATCH=false, 跳过"
fi

# ── 步骤 15: 生成 project.conf ────────────────────────────────────────────────
step "15. 生成 project.conf"
cd "$YAKIT_DIR"
python3 CONFIGS/gen_config.py \
  "PROJECTS/$PROJECT" \
  --device-conf "CONFIGS/${DEVICE}.conf"

# ── 步骤 16: 重打包 super.img ─────────────────────────────────────────────────
step "16. 重打包 super.img"
cd "$YAKIT_DIR"
sudo python3 yucca_android_kitchen.py --conf="\
  ACTION=super_repack,\
  PROJECT_NAME=$PROJECT,\
  OUTPUT_IMAGE=PROJECTS/$PROJECT/Build/super.img,\
  VBMETA_DIR=$EXTRACTED_DIR"
ok "super.img 重打包完成"
ls -lh "PROJECTS/$PROJECT/Build/"

# ── 步骤 17: 收集镜像到 Build/ ────────────────────────────────────────────────
step "17. 收集镜像"
SUPER_LOGICAL=(system product vendor odm system_ext system_dlkm vendor_dlkm mi_ext)
for img in "$EXTRACTED_DIR/"*.img; do
  [[ ! -f "$img" ]] && continue
  NAME=$(basename "$img" .img)
  [[ "$NAME" == "super" ]] && continue
  BASE="${NAME%_a}"; BASE="${BASE%_b}"
  SKIP=false
  for lp in "${SUPER_LOGICAL[@]}"; do
    [[ "$BASE" == "$lp" ]] && SKIP=true && break
  done
  $SKIP && continue
  if [[ "$NAME" == "init_boot" && -f "$BUILD_DIR/init_boot.img" ]]; then
    warn "init_boot.img 已由 KSU 处理, 保留 Build/ 版本"
    continue
  fi
  cp "$img" "$BUILD_DIR/${NAME}.img"
  echo "  [✓] ${NAME}.img"
done
ls -lh "$BUILD_DIR/"

# ── 步骤 18: VBMeta All ───────────────────────────────────────────────────────
step "18. Build VBMeta"
cd "$YAKIT_DIR"
for vb in vbmeta vbmeta_system; do
  SRC="$EXTRACTED_DIR/${vb}.img"
  if [[ -f "$SRC" ]]; then
    sudo python3 yucca_android_kitchen.py --conf="\
      ACTION=patch_vbmeta,\
      VBMETA_SOURCE=$SRC,\
      VBMETA_OUTPUT=$BUILD_DIR/${vb}.img,\
      VBMETA_PARTS_DIR=$BUILD_DIR,\
      VBMETA_FLAGS=3"
    ok "${vb}.img"
  fi
done


# ── 步骤 20: 打包 ─────────────────────────────────────────────────────────────
step "20. 打包刷机 ZIP"
TIMESTAMP=$(date +%Y%m%d)
OUTPUT_NAME="YuccaROM_${CODENAME}_${VERSION}_${TIMESTAMP}"
PKG="/tmp/yucca_pkg_$$"
OUT_DIR="$PROJECT_DIR/Output"
rm -rf "$PKG"
mkdir -p "$PKG/images" "$PKG/META-INF/com/google/android" "$OUT_DIR"

# ── super.img → super.img.zst ──
echo "[*] 压缩 super.img..."
zstd -T0 -1 "$BUILD_DIR/super.img" -o "$PKG/super.img.zst"
rm -f "$BUILD_DIR/super.img"
echo "  super.img.zst: $(stat -c%s "$PKG/super.img.zst" | numfmt --to=iec)"

# ── arm64 静态 zstd (卡刷包内置) ──
echo "[*] 编译 arm64 静态 zstd..."
if command -v aarch64-linux-gnu-gcc &>/dev/null; then
  # 先扫描 /tmp 下已有任意版本的编译产物, 优先复用避免网络依赖
  ZSTD_PREBUILT=$(find /tmp -maxdepth 3 -name "zstd" -path "*/programs/zstd" \
    -perm /111 2>/dev/null | head -1 || true)

  if [[ -n "$ZSTD_PREBUILT" ]]; then
    echo "  (复用已编译的 $ZSTD_PREBUILT)"
    cp "$ZSTD_PREBUILT" "$PKG/META-INF/zstd_arm64"
    chmod 755 "$PKG/META-INF/zstd_arm64"
    ok "zstd_arm64 (prebuilt) → META-INF/"
  else
    # 需要从网络下载并编译
    # GitHub API 有时被 rate-limit, 提供 fallback 版本
    # 注意: 脚本启用 pipefail, grep 无匹配时退出码 1, 必须用 || true
    ZSTD_VER=$( (curl -sL --max-time 15 "https://api.github.com/repos/facebook/zstd/releases/latest" \
      2>/dev/null || true) | grep -oP '"tag_name":\s*"v?\K[^"]+' | head -1 || true )
    if [[ -z "$ZSTD_VER" ]]; then
      ZSTD_VER="1.5.7"
      warn "GitHub API 不可用, 使用 fallback ZSTD_VER=$ZSTD_VER"
    fi
    rm -rf "/tmp/zstd-${ZSTD_VER}"
    if ! curl -fsSL --connect-timeout 20 --max-time 180 \
        "https://github.com/facebook/zstd/releases/download/v${ZSTD_VER}/zstd-${ZSTD_VER}.tar.gz" \
        | tar xz -C /tmp; then
      warn "zstd 源码下载失败 (v${ZSTD_VER}), 跳过内置 arm64 zstd"
      warn "卡刷时将依赖 Recovery 自带 zstd (TWRP 3.7+ 通常已内置)"
    else
      if ! make -j"$(nproc)" -C "/tmp/zstd-${ZSTD_VER}/programs" zstd \
          CC=aarch64-linux-gnu-gcc LDFLAGS="-static" >/dev/null 2>&1; then
        warn "zstd arm64 交叉编译失败, 跳过内置 arm64 zstd"
      else
        cp "/tmp/zstd-${ZSTD_VER}/programs/zstd" "$PKG/META-INF/zstd_arm64"
        chmod 755 "$PKG/META-INF/zstd_arm64"
        ok "zstd_arm64 (v${ZSTD_VER}) → META-INF/"
      fi
    fi
  fi
else
  warn "aarch64-linux-gnu-gcc 未安装, 跳过 arm64 zstd 编译"
  warn "卡刷时将依赖 Recovery 自带 zstd (TWRP 3.7+ 通常已内置)"
  warn "如需内置: sudo apt-get install gcc-aarch64-linux-gnu 后重新打包"
fi

# ── images/ ──
echo "[*] 收集 images/..."
for img in "$BUILD_DIR/"*.img; do
  [[ ! -f "$img" ]] && continue
  NAME=$(basename "$img")
  [[ "$NAME" == "super.img" ]] && continue
  [[ "$NAME" =~ _[ab]\.img$ ]] && continue   # 跳过 super_repack 中间文件
  mv "$img" "$PKG/images/$NAME"
  echo "  images/$NAME"
done
rm -rf "$BUILD_DIR"

# ── bat 脚本 ──
cp "$YAKIT_DIR/scripts/flash_all.bat"                  "$PKG/"
cp "$YAKIT_DIR/scripts/flash_all_except_storage.bat"   "$PKG/"
cp "$YAKIT_DIR/scripts/Tool.bat"                       "$PKG/"

# ── META-INF (卡刷脚本) ──
cp "$YAKIT_DIR/scripts/flash_package/update-binary"    "$PKG/META-INF/com/google/android/"
cp "$YAKIT_DIR/scripts/flash_package/updater-script"   "$PKG/META-INF/com/google/android/"

# ── Windows platform-tools (必须) ──
echo "[*] 下载 Windows platform-tools..."
_PT_URL="https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
for _retry in 1 2 3; do
  if curl -fSL --connect-timeout 15 --max-time 120 "$_PT_URL" \
      -o /tmp/platform-tools.zip 2>/tmp/pt_curl.log; then
    unzip -joq /tmp/platform-tools.zip \
      "platform-tools/adb.exe" \
      "platform-tools/AdbWinApi.dll" \
      "platform-tools/AdbWinUsbApi.dll" \
      "platform-tools/fastboot.exe" \
      -d "$PKG/"
    ok "adb.exe + fastboot.exe"
    rm -f /tmp/platform-tools.zip
    break
  else
    warn "platform-tools 下载失败 (第 $_retry 次): $(cat /tmp/pt_curl.log)"
    [[ $_retry -eq 3 ]] && { err "platform-tools 下载 3 次均失败，终止构建"; exit 1; }
  fi
done

# ── zstd.exe (必须) ──
echo "[*] 下载 zstd.exe..."
ZSTD_VER=${ZSTD_VER:-$( (curl -sL --max-time 15 "https://api.github.com/repos/facebook/zstd/releases/latest" \
  2>/dev/null || true) | grep -oP '"tag_name":\s*"v?\K[^"]+' | head -1 || true )}
ZSTD_VER=${ZSTD_VER:-1.5.7}
for _retry in 1 2 3; do
  if curl -fSL --connect-timeout 15 --max-time 60 \
      "https://github.com/facebook/zstd/releases/download/v${ZSTD_VER}/zstd-v${ZSTD_VER}-win64.zip" \
      -o /tmp/zstd_win.zip 2>/tmp/zstd_curl.log; then
    unzip -joq /tmp/zstd_win.zip "*/zstd.exe" -d "$PKG/"
    ok "zstd.exe (v${ZSTD_VER})"
    rm -f /tmp/zstd_win.zip
    break
  else
    warn "zstd.exe 下载失败 (第 $_retry 次): $(cat /tmp/zstd_curl.log)"
    [[ $_retry -eq 3 ]] && { err "zstd.exe 下载 3 次均失败，终止构建"; exit 1; }
  fi
done

# ── KernelSU.apk (KSU_PATCH=true 时内置到刷机包，供用户刷机后安装) ──
if [[ "$KSU_PATCH" == "true" && "$SKIP_KSU" == "false" ]]; then
  echo "[*] 下载 KernelSU.apk..."
  _KSU_APK_OK=false
  for _retry in 1 2 3; do
    KSU_URL=$( (curl -sL --max-time 15 "https://api.github.com/repos/tiann/KernelSU/releases/latest" \
      2>/dev/null || true) | grep -oP '"browser_download_url":\s*"\K[^"]*\.apk' | head -1 || true)
    if [[ -n "$KSU_URL" ]]; then
      if curl -fSL --connect-timeout 15 --max-time 120 "$KSU_URL" \
          -o "$PKG/KernelSU.apk" 2>/tmp/ksu_curl.log; then
        ok "KernelSU.apk"
        # 同时更新本地缓存（方便下次离线使用）
        cp "$PKG/KernelSU.apk" "$YAKIT_DIR/.bin/.ksu_cache/v${KSU_URL##*/v}_KernelSU.apk" 2>/dev/null || true
        _KSU_APK_OK=true
        break
      else
        warn "KernelSU.apk 下载失败 (第 $_retry 次): $(cat /tmp/ksu_curl.log)"
      fi
    else
      warn "KernelSU.apk 链接获取失败 (第 $_retry 次)，重试..."
    fi
  done
  # 网络失败时回退到本地缓存
  if [[ "$_KSU_APK_OK" == "false" ]]; then
    _KSU_APK_CACHE=$(find "$YAKIT_DIR/.bin/.ksu_cache" -name "*_KernelSU.apk" 2>/dev/null \
      | sort -rV | head -1 || true)
    if [[ -n "$_KSU_APK_CACHE" ]]; then
      cp "$_KSU_APK_CACHE" "$PKG/KernelSU.apk"
      warn "KernelSU.apk 下载失败，已使用本地缓存: $(basename "$_KSU_APK_CACHE")"
    else
      warn "KernelSU.apk 下载失败且无本地缓存，刷机包中将不含 KernelSU.apk"
    fi
  fi
fi

# ── 打包 ZIP ──
echo "[*] 打包 ${OUTPUT_NAME}.zip..."
cd "$PKG"
zip -r -1 "$OUT_DIR/${OUTPUT_NAME}.zip" . -x "*.DS_Store" -x "__MACOSX/*"
rm -rf "$PKG"

# ── 步骤 19: 释放空间（打包完成后）──────────────────────────────────────────────
step "19. 释放空间"
# 清理构建中间产物（ROM 解包目录、逻辑分区临时目录、Extracted/）
# 不删 INPUT/（保留原始 ZIP）；不删 Output/（成品刷机包）
rm -rf "$PROJECT_DIR/ROM" "$PROJECT_DIR/_logical_temp" "$EXTRACTED_DIR"
# 清理从 YAK-Profiles 复制到 YAKit 的临时文件
rm -rf "$YAKIT_DIR/scripts" "$YAKIT_DIR/CONFIGS" "$YAKIT_DIR/fixes" "$YAKIT_DIR/apks"
# 清理 /tmp 中的编译和下载缓存（不删正在写入的日志文件）
rm -rf /tmp/zstd-* /tmp/yucca_build_*.log
ok "中间产物已清理"
df -h /

ARCHIVE="$OUT_DIR/${OUTPUT_NAME}.zip"
ARCHIVE_SIZE=$(stat -c%s "$ARCHIVE" | numfmt --to=iec)
ok "产物: $ARCHIVE ($ARCHIVE_SIZE)"


# ── 完成 ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${GREEN}  构建完成!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo "  设备: $DEVICE_NAME ($CODENAME)"
echo "  版本: $VERSION"
echo "  输出: $ARCHIVE"
echo ""
echo "  卡刷: adb sideload $ARCHIVE"
echo "       或拷贝到手机后 TWRP → Install"
echo ""
