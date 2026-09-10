#!/usr/bin/env bash
# 出一个发布件（tar）：按 `平台 × 后端` 各出一份。
#
#   bash make-release.sh amd64-cpu
#   bash make-release.sh arm64-rknn
#
# ★wheel 是**平台相关**的（带 C 扩展的每个"平台 × Python 版本"一个文件），
#   所以不能只出一份包 —— 这与 historystore 的 amd64/arm64 双投放同形。
# ★e52c 的 pypi **不可达**（实测），故 wheelhouse 必须离线带齐。
set -euo pipefail
TARGET="${1:?用法: make-release.sh <amd64-cpu|arm64-rknn>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
OUT="$SRC/dist/aiintegration-$TARGET"

case "$TARGET" in
  amd64-cpu)  PLAT=(--platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64); PYV=3.11 ;;
  arm64-rknn) PLAT=(--platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64); PYV=3.11 ;;
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

TAR="$SRC/dist/aiintegration-$TARGET.tar.gz"
tar -C "$SRC/dist" -czf "$TAR" "aiintegration-$TARGET"
echo "=== 产物 ==="
ls -la "$TAR" | awk '{print "  ", $5, $9}'
echo "  wheelhouse: $(ls "$OUT/wheelhouse" | wc -l) 个 wheel，$(du -sm "$OUT/wheelhouse" | cut -f1) MB"
echo "  sha256: $(sha256sum "$TAR" | cut -d' ' -f1)"
