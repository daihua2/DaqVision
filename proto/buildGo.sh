#!/usr/bin/env bash
# 生成 Go gRPC 桩代码。两种用法：
#   1) 本地参考生成（默认输出到 ./gen）：    ./buildGo.sh
#   2) 直接生成进 daqgate（推荐，由 daqgate 侧 agent 在 WSL 执行）：
#        ./buildGo.sh /mnt/d/Project/daqgate/ProtocolGate/proto/visionpb
# 依赖：protoc + protoc-gen-go + protoc-gen-go-grpc（与 daqgate 现有 proto/build.sh 一致）
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
OUT="${1:-./gen}"
mkdir -p "$OUT"
echo "生成 Go 桩 -> $OUT"
protoc -I. \
  --go_out="$OUT" \
  --go-grpc_out="$OUT" \
  vision.proto
echo "完成。daqgate 侧：在 channel/vision 里 import 该 visionpb 包，作为 gRPC client 连 Python 服务。"
