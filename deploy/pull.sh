#!/usr/bin/env bash
# С ноутбука: забрать результаты с машины в results-aws/ (можно запускать
# сколько угодно раз во время серии — rsync докачивает только новое).
#   bash deploy/pull.sh <публичный-IP-или-DNS>
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${1:?укажите адрес машины}
KEY=${KEY:-~/.ssh/gosched-bench.pem}
mkdir -p results-aws
rsync -az --timeout=120 -e "ssh -i $KEY -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4" "ubuntu@$HOST:gosched-bench/results/" results-aws/
du -sh results-aws
