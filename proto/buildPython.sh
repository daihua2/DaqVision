#!/usr/bin/env bash
# 生成 Python gRPC 桩代码到 vision-infer/app/，供推理服务 import。
# 依赖：pip install grpcio-tools
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
OUT="../vision-infer/app"
echo "生成 Python 桩 -> $OUT"
python -m grpc_tools.protoc \
  -I. \
  --python_out="$OUT" \
  --grpc_python_out="$OUT" \
  vision.proto
echo "完成：$OUT/vision_pb2.py, $OUT/vision_pb2_grpc.py"
