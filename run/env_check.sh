#!/usr/bin/env bash
# Проверка и фиксация окружения перед серией прогонов.
# Печатает отчёт (run.py сохраняет его в meta.json каждого прогона).
# Код выхода 1, если окружение не годится для измерений; FORCE=1 — не падать.
set -uo pipefail

fail=0
warn() { echo "WARN: $*"; fail=1; }

echo "date: $(date -Is)"
echo "host: $(hostname)"
echo "uname: $(uname -a)"
echo "go: $(go version 2>/dev/null || echo 'нет go')"

if [[ "$(uname)" != "Linux" ]]; then
  warn "не Linux: для статьи прогоны идут на Linux (cgroup, губернатор, taskset). Здесь только отладка."
  [[ "${FORCE:-0}" == 1 ]] && exit 0 || exit $fail
fi

echo "cpus: $(nproc)"
lscpu | grep -E 'Model name|Thread\(s\) per core|Core\(s\) per socket|Socket\(s\)|NUMA node\(s\)' | sed 's/^/lscpu: /'

# Губернатор частоты: нужен performance на всех ядрах.
govs=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u | tr '\n' ' ')
echo "governor: ${govs:-нет cpufreq}"
[[ -n "$govs" && "$govs" != "performance " ]] && warn "губернатор не performance: sudo cpupower frequency-set -g performance"

# Turbo/boost: должен быть выключен (или хотя бы зафиксирован в отчёте).
if [[ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
  nt=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo); echo "intel no_turbo: $nt"
  [[ "$nt" != 1 ]] && warn "turbo включён: echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo"
elif [[ -f /sys/devices/system/cpu/cpufreq/boost ]]; then
  b=$(cat /sys/devices/system/cpu/cpufreq/boost); echo "cpufreq boost: $b"
  [[ "$b" != 0 ]] && warn "boost включён: echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost"
fi

# SMT: фиксируем состояние (выключать не обязательно, но описать в статье).
[[ -f /sys/devices/system/cpu/smt/active ]] && echo "smt active: $(cat /sys/devices/system/cpu/smt/active)"

# Изолированные ядра (isolcpus) — желательно для сервера и генератора.
echo "isolated: $(cat /sys/devices/system/cpu/isolated 2>/dev/null)"

# cgroup v2 нужна для фактора C (CPU-лимит через systemd-run).
[[ -f /sys/fs/cgroup/cgroup.controllers ]] && echo "cgroup: v2" || warn "нет cgroup v2: фактор C недоступен"

[[ "${FORCE:-0}" == 1 ]] && exit 0
exit $fail
