#!/usr/bin/env bash
# 出一个发布件（tar）：按 `平台 × 后端` 各出一份。
#
#   PYV=3.10 bash make-release.sh amd64-cpu      # AISERVER（2026-09-11 现查：x86_64 / Python 3.10.12）
#   PYV=3.11 bash make-release.sh arm64-rknn     # e52c 一类（版本以现场 python3 -V 为准）
#
# ★wheel 是**平台相关**的（带 C 扩展的每个"平台 × Python 版本"一个文件），
#   所以不能只出一份包 —— 这与 historystore 的 amd64/arm64 双投放同形。
# ★e52c 的 pypi **不可达**（实测），故 wheelhouse 必须离线带齐。
#
# ★**Python 版本没有缺省，必须显式给 `PYV`**。
#   这里曾写死 `3.11`，而 AISERVER 现场是 **3.10.12** —— grpcio 是 C 扩展、按 Python 版本分 wheel，
#   那样出的包在目标机上 `pip install --no-index` **会直接失败**。版本是目标机的事实，
#   只能现查，猜一个缺省就是把"装不上"推迟到现场。
# ★给了的版本会写进发布件（`wheelhouse/PYTHON_TAG`），`install.sh` 在动任何东西之前先核对。
set -euo pipefail
TARGET="${1:?用法: PYV=<目标机 python 主.次> make-release.sh <amd64-cpu|arm64-rknn>}"
PYV="${PYV:?必须显式给 PYV（目标机上 python3 -V 的主.次，如 3.10）—— 没有缺省，见脚本头}"
case "$PYV" in
  3.[0-9]|3.[0-9][0-9]) ;;
  *) echo "PYV 格式应为 主.次（如 3.10），收到: $PYV" >&2; exit 1 ;;
esac
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
OUT="$SRC/dist/aiintegration-$TARGET-py$PYV"

case "$TARGET" in
  amd64-cpu)  PLAT=(--platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64); ARCH=x86_64 ;;
  arm64-rknn) PLAT=(--platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64); ARCH=aarch64 ;;
  *) echo "未知目标: $TARGET" >&2; exit 1 ;;
esac

rm -rf "$OUT"; mkdir -p "$OUT/wheelhouse"
cp -r "$SRC/host" "$OUT/host"
find "$OUT/host" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$OUT/domains"
[ -d "$SRC/domains" ] && cp -r "$SRC/domains/." "$OUT/domains/" || true
cp "$SRC/host/requirements.txt" "$OUT/"
cp "$HERE/install.sh" "$HERE/aiintegration.service" "$OUT/"

echo "=== 抓 wheel（目标平台 $TARGET, cp$PYV）==="
python -m pip download -r "$OUT/requirements.txt" -d "$OUT/wheelhouse" \
    "${PLAT[@]}" --python-version "$PYV" --implementation cp --only-binary=:all:

# 发布件自报"我是给哪个 Python、哪个架构出的"——install.sh 开工前先核对这两项。
printf '%s\n' "$PYV" > "$OUT/wheelhouse/PYTHON_TAG"
printf '%s\n' "$ARCH" > "$OUT/wheelhouse/ARCH_TAG"

TAR="$SRC/dist/aiintegration-$TARGET-py$PYV.tar.gz"
tar -C "$SRC/dist" -czf "$TAR" "aiintegration-$TARGET-py$PYV"
echo "=== 产物 ==="
ls -la "$TAR" | awk '{print "  ", $5, $9}'
echo "  wheelhouse: $(ls "$OUT/wheelhouse" | wc -l) 个 wheel，$(du -sm "$OUT/wheelhouse" | cut -f1) MB"
echo "  sha256: $(sha256sum "$TAR" | cut -d' ' -f1)"
