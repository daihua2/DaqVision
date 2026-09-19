#!/usr/bin/env bash
# 跑全部 SCA 实验，输出落 out/（不入库的大文件一个都不写，只写文本结果）
# 用法：bash run_all.sh /mnt/d/download/AIRef/datasets/sca
set -euo pipefail
cd "$(dirname "$0")"
DATA="${1:?数据目录}"
PY=~/research-venv/bin/python
mkdir -p out
$PY -W ignore sca_baseline.py "$DATA"           | tee out/1_velocity_baseline.txt
$PY -W ignore sca_recent.py "$DATA"             | tee out/2_velocity_recent.txt
for f in env kurt crest; do
  $PY -W ignore sca_envelope.py "$DATA" "$f"   | tee "out/3_${f}.txt"
done
$PY -W ignore sca_methods.py "$DATA"            | tee out/4_methods.txt
$PY -W ignore sca_rule.py "$DATA"               | tee out/5_rule.txt
