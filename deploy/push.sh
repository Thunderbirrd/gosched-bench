#!/usr/bin/env bash
# С ноутбука: залить код стенда на машину (без bin/ и results/).
#   bash deploy/push.sh <публичный-IP-или-DNS>
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${1:?укажите адрес машины}
KEY=${KEY:-~/.ssh/gosched-bench.pem}
rsync -az --delete -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" \
  --exclude bin/ --exclude results/ --exclude .toolchain/ --exclude .idea/ --exclude __pycache__/ \
  --exclude 'results-*.tar.gz' \
  ./ "ubuntu@$HOST:gosched-bench/"
echo "залито. Дальше: ssh -i $KEY ubuntu@$HOST 'cd gosched-bench && bash deploy/setup.sh'"
