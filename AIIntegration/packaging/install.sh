#!/usr/bin/env bash
# 在目标机上安装/升级 AIIntegration。
#
#   发布件（tar 解开后的样子）
#     install.sh          ← 本脚本
#     host/               骨架
#     domains/            域模块（.py）
#     wheelhouse/         该平台的全部 .whl
#     requirements.txt
#     aiintegration.service
#
# 设计要点：
#   · **现场建 venv，不搬 venv** —— venv 里的脚本与 pyvenv.cfg 写死绝对路径，跨机拷贝易坏；
#   · **离线安装**（`--no-index`）—— e52c 的 pypi 不可达，实测；
#   · **自建独立 venv，绝不共用** —— AISERVER 上 v5 与 VFD 共用 v4 的 .venv，
#     动一下三个在跑的项目一起遭殃。本服务不掺和。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${AII_ROOT:-/home/Project/AIIntegration}"
VENV="$ROOT/.venv"
PY="${PYTHON:-python3}"

echo "=== AIIntegration 安装 → $ROOT ==="
"$PY" -V

mkdir -p "$ROOT"

# ── 1. venv（现场建）──────────────────────────────────────────────────────
if [ ! -x "$VENV/bin/python" ]; then
  echo "--- 建独立 venv: $VENV"
  "$PY" -m venv "$VENV"
else
  echo "--- 复用已有 venv: $VENV"
fi

# ── 2. 离线装依赖 ────────────────────────────────────────────────────────
if [ -d "$HERE/wheelhouse" ]; then
  echo "--- 从 wheelhouse 离线安装（不联网）"
  "$VENV/bin/pip" install --no-index --find-links="$HERE/wheelhouse" \
      -r "$HERE/requirements.txt" --upgrade
else
  # 有网的机器上也走同一套流程，只是允许联网 —— 两种机制会漂，一套就够。
  echo "--- 未见 wheelhouse，退回联网安装（e52c 上不会走到这条）"
  "$VENV/bin/pip" install -r "$HERE/requirements.txt" --upgrade
fi

# ── 3. 铺代码（★不碰 data/ 与 system.guid）───────────────────────────────
echo "--- 铺代码"
rm -rf "$ROOT/host"
cp -r "$HERE/host" "$ROOT/host"
mkdir -p "$ROOT/domains"
# 域模块：**只补不删** —— 现场可能手工放过域，升级不该把它抹掉。
if [ -d "$HERE/domains" ]; then
  cp -r "$HERE/domains/." "$ROOT/domains/"
fi
mkdir -p "$ROOT/data" "$ROOT/cert"

# ★ system.guid 一个字都不碰：它一经生成永不变。
#   换掉它 = 先前写入的点变成无主数据，而按对账铁律**绝不自动删**，残留清不掉。
if [ -f "$ROOT/system.guid" ]; then
  echo "--- 保留既有身份: $(cat "$ROOT/system.guid")"
fi

# ── 4. systemd ──────────────────────────────────────────────────────────
if [ -d /etc/systemd/system ] && [ -f "$HERE/aiintegration.service" ]; then
  echo "--- 注册 systemd 单元"
  install -m644 "$HERE/aiintegration.service" /etc/systemd/system/aiintegration.service
  systemctl daemon-reload
  echo "    启用并启动：systemctl enable --now aiintegration.service"
  echo "    ★停启一律走 systemctl，别手工 kill + 拉起。"
fi

echo "=== 安装完成 ==="
echo "配置走环境变量（drop-in: /etc/systemd/system/aiintegration.service.d/override.conf）："
echo "  AII_HS_READ   读连接（明文回环全量口），缺省 127.0.0.1:5400"
echo "  AII_HS_WRITE  写连接（mTLS 跨机口）—— **不配就是只读运行，结论不回流**"
echo "  AII_CERT_DIR  客户端证书目录（ca.cer / client.cer / client.key），缺省 \$AII_ROOT/cert"
