#!/usr/bin/env bash
# Серия «ноутбук» для сравнения окружений (статья 2): та же конфигурация, что envcmp
# на AWS, но нативно на macOS — без привязки к ядрам и без настройки ОС.
#   caffeinate -i bash deploy/run_mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${OUT:-results-mac}
SPIN_AHEAD=${SPIN_AHEAD:-2ms}
mkdir -p "$OUT"
go build -o bin/ ./cmd/...
{ FORCE=1 bash run/env_check.sh; sysctl -n machdep.cpu.brand_string hw.ncpu hw.perflevel0.physicalcpu hw.perflevel1.physicalcpu; sw_vers; } > "$OUT/env.txt" 2>&1 || true
./bin/calib > "$OUT/calib.json"
[[ -f "$OUT/overhead/overhead.json" ]] || python3 run/overhead.py --out "$OUT/overhead" --spin-ahead "$SPIN_AHEAD" --dur 15 --reps 2
OH=$(python3 -c "import json; print(json.load(open('$OUT/overhead/overhead.json'))['overhead_us'])")
python3 run/make_plan.py --ci --reps 20 > "$OUT/plan-envcmp.csv"
python3 run/run.py --plan "$OUT/plan-envcmp.csv" --out "$OUT/envcmp" --spin-ahead "$SPIN_AHEAD" \
  --warmup 5 --dur 30 --lc-overhead-us "$OH" --no-build --raw
python3 analysis/collect.py "$OUT/envcmp" | tail -3
echo "готово"
