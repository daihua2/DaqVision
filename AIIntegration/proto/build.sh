#!/usr/bin/env bash
# 从 aiintegration.proto 生成 Python 桩到 host/aiintegration/apiproto/。
#
# ★必须用**系统 protoc**：`python3 -m grpc_tools.protoc` 捆的是 libprotoc 3.5.1（太老）。
# ★只生成 `_pb2.py`：服务端用 `grpc.method_handlers_generic_handler` 手接，
#   不需要 grpc 的 python 插件 —— 少一个构建期依赖，e52c 那侧发布也少一件事。
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/../host/aiintegration/apiproto"

command -v protoc >/dev/null || { echo "缺 protoc（Debian: apt install protobuf-compiler）" >&2; exit 1; }
mkdir -p "$OUT"
protoc -I"$HERE" --python_out="$OUT" aiintegration.proto
touch "$OUT/__init__.py"
echo "生成：$OUT/aiintegration_pb2.py"

# 分发副本（贵方检不出本仓，故投一份到 AISERVER 的分发点）——
# ★**改即投**：不投就会出现"我方以为改了、贵方发版还是旧的"，而且不报错（C-9 §2.1）。
echo "★别忘了投分发点并发函：AISERVER:/home/Project/AIIntegration/proto/aiintegration.proto"
