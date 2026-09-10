#!/usr/bin/env bash
# 从 historystore 仓重新取契约并生成 Python 桩。
#
# ★必须用**系统 protoc**：`python3 -m grpc_tools.protoc` 捆的是 libprotoc 3.5.1，
#   编不过 proto3 的 `optional`（historystore.proto 里有十多处）。
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HS="${HS_REPO:-$HERE/../../../../../historystore}"

if [ ! -f "$HS/proto/historystore.proto" ]; then
  echo "找不到 historystore 仓：$HS" >&2
  echo "用 HS_REPO=/path/to/historystore bash regen.sh 指定" >&2
  exit 1
fi

command -v protoc >/dev/null || { echo "缺 protoc（Debian: apt install protobuf-compiler）" >&2; exit 1; }

cp "$HS/proto/daqcontract.proto" "$HS/proto/historystore.proto" "$HERE/"
protoc -I"$HERE" --python_out="$HERE" daqcontract.proto historystore.proto

# 生成物默认是顶层 import（`import daqcontract_pb2`），改成包内相对 import，
# 免得污染 sys.path 或与别处同名模块撞车。
sed -i 's/^import daqcontract_pb2 as/from . import daqcontract_pb2 as/' "$HERE/historystore_pb2.py"

rev=$(cd "$HS" && git rev-parse --short HEAD)
sed -i "s/^来源提交：.*/来源提交：$rev/" "$HERE/PROVENANCE.txt"
sed -i "s/^取用日期：.*/取用日期：$(date +%F)/" "$HERE/PROVENANCE.txt"
echo "已更新到 historystore $rev；生成：daqcontract_pb2.py historystore_pb2.py"
