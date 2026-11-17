#!/usr/bin/env bash
# Быстрая проверка стенда на одной машине: сервер с BE-нагрузкой + генератор.
# Результаты не годятся для статьи (генератор и сервер делят CPU), только для
# проверки, что всё собирается, запускается и пишет файлы.
#
#   ./run/smoke.sh            # GOMAXPROCS сервера = 2
#   P=4 RATE=5000 ./run/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."

P=${P:-2}
RATE=${RATE:-2000}
BE_UTIL=${BE_UTIL:-0.4}
BE_Q=${BE_Q:-20ms}
OUT=${OUT:-results/smoke-$(date +%Y%m%d-%H%M%S)}
ADDR=127.0.0.1:${PORT:-7070}

go build -o bin/ ./cmd/...
mkdir -p "$OUT"

GOMAXPROCS=$P ./bin/server -addr "$ADDR" -out "$OUT/server" \
  -be-util "$BE_UTIL" -be-q "$BE_Q" >"$OUT/server.log" 2>&1 &
SP=$!
trap 'kill $SP 2>/dev/null || true' EXIT

for _ in $(seq 100); do grep -q READY "$OUT/server.log" && break; sleep 0.1; done
grep -q READY "$OUT/server.log" || { echo "сервер не стартовал"; cat "$OUT/server.log"; exit 1; }

WARMUP=2
(sleep $WARMUP && kill -USR1 $SP) &
GOMAXPROCS=1 ./bin/loadgen -addr "$ADDR" -rate "$RATE" -warmup ${WARMUP}s -dur 6s \
  -out "$OUT/loadgen" -lag-max 1ms
kill -TERM $SP
wait $SP || true

echo "--- $OUT"
ls -1 "$OUT"/server "$OUT"/loadgen
