#!/usr/bin/env bash
# 在 WSL 里跑 AIIntegration 单测：三环境**串行**（整机用例绑固定端口，不能并行）。
#
#   bash run-tests.sh            # 三个都跑：3.12 → py310 → vision
#   bash run-tests.sh py310      # 只跑一个：312 | py310 | vision
#
# ★先清代理变量：WSL 继承了 http_proxy=127.0.0.1:10808，而 no_proxy 的 `127.*` 通配
#   Python urllib 不认 ⇒ 访问 127.0.0.1 的 HTTP 用例全走代理回 502，看着像代码坏了。
set -uo pipefail
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

cd "$(dirname "${BASH_SOURCE[0]}")"

declare -A PY=(
  [312]=/usr/bin/python3
  [py310]="$HOME/aii-py310/bin/python"
  [vision]="$HOME/aii-vision/bin/python"
)
ORDER=(312 py310 vision)
[ $# -gt 0 ] && ORDER=("$@")

failed=()
for env in "${ORDER[@]}"; do
  py="${PY[$env]:-}"
  [ -n "$py" ] || { echo "未知环境: $env（可选 312 | py310 | vision）" >&2; exit 2; }
  [ -x "$py" ] || { echo "[$env] 解释器不存在: $py" >&2; failed+=("$env"); continue; }
  echo "===== [$env] $("$py" -V 2>&1) ====="
  "$py" -m unittest discover -s tests || failed+=("$env")
done

if [ ${#failed[@]} -gt 0 ]; then
  echo "===== 未通过: ${failed[*]} =====" >&2
  exit 1
fi
echo "===== 全部通过: ${ORDER[*]} ====="
