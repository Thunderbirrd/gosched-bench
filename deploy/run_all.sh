#!/usr/bin/env bash
# Все серии обеих статей подряд. Запускать в tmux на подготовленной машине:
#
#   tmux new -s bench 'mkdir -p results && bash deploy/run_all.sh 2>&1 | tee -a results/run_all.log'
#
# Порядок: калибровка → накладные расходы (поправка ρ) → короткие серии
# (абляции, статья 2) → основная серия статьи 1. Короткие идут раньше, чтобы
# проблема стенда всплыла в первые часы, а не к концу основной серии.
#
# Каждый шаг можно повторить: run.py пропускает готовые прогоны, а шаги
# калибровки и накладных — если их результат уже есть. Чтобы выполнить
# только часть: PHASES="calib overhead co" bash deploy/run_all.sh
#
# Вторая сессия:
#   c7g.metal (H1 по режимам):   PHASES="env calib co2 h1 h1trace ci6 pack" bash deploy/run_all.sh
#   c7g.8xlarge (ВМ, статья 2):  LOADGEN_CPUS=20-31 PHASES="env calib overhead envcmp pack" bash deploy/run_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/usr/local/go/bin:$PATH GOTOOLCHAIN=local
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}

# Раскладка ядер (c7g.metal, 64 ядра без SMT). Служебные 0-3 — ОС и прерывания.
SERVER_CPUS=${SERVER_CPUS:-4-19}    # GOMAXPROCS = P ≤ 8; остальные ядра — для sysmon и сетевых потоков
LOADGEN_CPUS=${LOADGEN_CPUS:-32-47} # генератор: до ~110 тыс. запросов/с при P = 8
SPIN_AHEAD=${SPIN_AHEAD:-2ms}       # sleep пересыпает до ~1 мс и на c7g.metal (100us давали невалидные прогоны)
OUT=${OUT:-results}
PHASES=${PHASES:-"env calib overhead ablation co ci main pack"}
WARMUP=${WARMUP:-5} DUR=${DUR:-30}  # для проверки скрипта: WARMUP=1 DUR=3 LIMIT=2
LIMIT=${LIMIT:-0}                   # 0 — все прогоны серии

RUN=(python3 run/run.py --server-cpus "$SERVER_CPUS" --loadgen-cpus "$LOADGEN_CPUS" --spin-ahead "$SPIN_AHEAD"
     --warmup "$WARMUP" --dur "$DUR" --limit "$LIMIT")
mkdir -p "$OUT"
log() { echo "[$(date '+%F %T')] $*"; }

overhead() { # мкс; если фаза overhead не выполнялась — 0 (номинальная ρ)
  if [[ -f "$OUT/overhead/overhead.json" ]]; then
    python3 -c "import json; print(json.load(open('$OUT/overhead/overhead.json'))['overhead_us'])"
  else
    echo 0
  fi
}

series() { # имя план [доп. аргументы run.py]
  local name=$1 plan=$2; shift 2
  log "серия $name: $(($(wc -l < "$plan") - 1)) прогонов"
  "${RUN[@]}" --plan "$plan" --out "$OUT/$name" --lc-overhead-us "$(overhead)" --no-build "$@"
  # сводка не должна останавливать следующие фазы: сырые прогоны уже на диске, collect можно повторить
  python3 analysis/collect.py "$OUT/$name" | tail -3 || log "collect для $name упал — повторить вручную"  # --sim — потом на ноутбуке
}

for ph in $PHASES; do
  case $ph in
    env)
      log "окружение"
      if command -v go >/dev/null; then go build -o bin/ ./cmd/...; else log "нет go — беру готовые bin/"; fi
      { FORCE=1 bash run/env_check.sh; echo "cmdline: $(cat /proc/cmdline)";
        echo "server_cpus: $SERVER_CPUS"; echo "loadgen_cpus: $LOADGEN_CPUS"; } > "$OUT/env.txt"
      cat "$OUT/env.txt"
      ;;
    calib)
      log "калибровка spin (на ядре сервера)"
      taskset -c "${SERVER_CPUS%%-*}" ./bin/calib > "$OUT/calib.json"
      python3 - "$OUT/calib.json" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1]))
print(f"разброс калибровки {c['calib_spread']*100:.2f}%")
for k in c["checks"]:
    print(f"  {k['target']}: медиана {k['p50_rel_err']*100:+.1f}%, P99 {k['p99_rel_err']*100:+.1f}%")
short = c["checks"][0]
if abs(short["p50_rel_err"]) > 0.2:
    print(f"ВНИМАНИЕ: spin на {short['target']} отклоняется на {short['p50_rel_err']*100:+.0f}% — "
          "поправка ρ всё равно берётся из измеренного CPU (фаза overhead), но в статье это надо оговорить")
if c["calib_spread"] > 0.03:
    sys.exit("разброс > 3%: стенд нестабилен, разберитесь до серий")
EOF
      ;;
    overhead)
      if [[ -f "$OUT/overhead/overhead.json" ]]; then
        log "накладные уже измерены: $(overhead) мкс"
      else
        log "накладные расходы на LC-запрос"
        python3 run/overhead.py --out "$OUT/overhead" --server-cpus "$SERVER_CPUS" \
          --loadgen-cpus "$LOADGEN_CPUS" --spin-ahead "$SPIN_AHEAD" --dur "$((DUR < 15 ? DUR : 15))" \
          --reps "$([[ $LIMIT -gt 0 ]] && echo 1 || echo 2)"
      fi
      ;;
    ablation) python3 run/make_plan.py --ablation > "$OUT/plan-ablation.csv"; series ablation "$OUT/plan-ablation.csv" ;;
    co)       python3 run/make_plan.py --co > "$OUT/plan-co.csv";             series co "$OUT/plan-co.csv" --raw ;;
    ci)       python3 run/make_plan.py --ci > "$OUT/plan-ci.csv";             series ci "$OUT/plan-ci.csv" --raw ;;
    main)     python3 run/make_plan.py > "$OUT/plan-main.csv";                series main "$OUT/plan-main.csv" ;;
    # --- вторая сессия ---
    h1)       python3 run/make_plan.py --h1 > "$OUT/plan-h1.csv";             series h1 "$OUT/plan-h1.csv" ;;
    h1trace)  # по одному прогону на точку с трассой планировщика; задержки отсюда в статью не идут
              python3 run/make_plan.py --h1 | tr -d '\r' | awk -F, 'NR==1 || $NF==0' > "$OUT/plan-h1trace.csv"
              series h1trace "$OUT/plan-h1trace.csv" --trace 1s ;;
    co2)      # повтор серии co после исправления закрытого режима генератора (16.11.2026)
              python3 run/make_plan.py --co > "$OUT/plan-co2.csv";            series co2 "$OUT/plan-co2.csv" --raw ;;
    ci6)      # близкая к A (q = 5 мс) конфигурация для кривой мощности, статья 2
              python3 run/make_plan.py --ci --q 6ms > "$OUT/plan-ci6.csv";    series ci6 "$OUT/plan-ci6.csv" --raw ;;
    envcmp)   python3 run/make_plan.py --ci --reps 20 > "$OUT/plan-envcmp.csv"; series envcmp "$OUT/plan-envcmp.csv" --raw ;;
    pack)
      f="results-$(hostname)-$(date +%Y%m%d-%H%M).tar.gz"
      log "архив $f"
      tar -czf "$f" "$OUT"
      ls -lh "$f"
      ;;
    *) echo "неизвестная фаза $ph"; exit 1 ;;
  esac
done
log "готово"
