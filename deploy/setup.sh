#!/usr/bin/env bash
# Однократная подготовка арендованной машины (Ubuntu, arm64 или amd64).
# Запускать от пользователя ubuntu (нужен sudo):
#
#   bash deploy/setup.sh
#
# Что делает:
#   1. ставит пакеты (tmux, rsync, python3) и Go нужной версии с проверкой sha256;
#   2. останавливает фоновые службы, которые дают шум (обновления, snap, таймеры apt);
#   3. уводит прерывания на служебные ядра HK_CPUS и выключает irqbalance;
#   4. ставит губернатор performance, если частотой можно управлять;
#   5. делегирует контроллер cpu пользовательским срезам systemd и включает linger,
#      чтобы run.py мог запускать сервер под CPU-лимитом (H5) через systemd-run --user;
#   6. собирает бинарники и печатает отчёт env_check.
#
# isolcpus НЕ используется: на изолированных ядрах ядро не балансирует потоки,
# и все потоки сервера, закреплённые taskset на наборе ядер, оказались бы на одном.
# Вместо этого — служебные ядра для ОС и прерываний и taskset для сервера/генератора.
set -euo pipefail
cd "$(dirname "$0")/.."

GO_VERSION=${GO_VERSION:-1.27.3}
HK_CPUS=${HK_CPUS:-0-3}   # служебные ядра: ОС, прерывания, ssh, run.py

case "$(uname -m)" in
  aarch64) GOARCH=arm64 ;;
  x86_64) GOARCH=amd64 ;;
  *) echo "неизвестная архитектура $(uname -m)"; exit 1 ;;
esac

echo "== пакеты"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmux rsync python3 curl ca-certificates >/dev/null

echo "== Go $GO_VERSION ($GOARCH)"
if ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go$GO_VERSION "; then
  tgz="go$GO_VERSION.linux-$GOARCH.tar.gz"
  curl -fsSL -o "/tmp/$tgz" "https://go.dev/dl/$tgz"
  want=$(curl -fsSL "https://go.dev/dl/?mode=json&include=all" | python3 -c "
import json, sys
for r in json.load(sys.stdin):
    for f in r['files']:
        if f['filename'] == '$tgz':
            print(f['sha256'])")
  got=$(sha256sum "/tmp/$tgz" | cut -d' ' -f1)
  [[ -n "$want" && "$want" == "$got" ]] || { echo "sha256 не совпал: $got != $want"; exit 1; }
  sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf "/tmp/$tgz"
fi
grep -q /usr/local/go/bin ~/.profile || echo 'export PATH=/usr/local/go/bin:$PATH' >> ~/.profile
export PATH=/usr/local/go/bin:$PATH
export GOTOOLCHAIN=local
go version

echo "== фоновые службы"
for s in unattended-upgrades apt-daily.timer apt-daily-upgrade.timer man-db.timer \
         fwupd-refresh.timer motd-news.timer snapd snapd.socket irqbalance; do
  sudo systemctl disable --now "$s" >/dev/null 2>&1 || true
done

echo "== прерывания -> ядра $HK_CPUS"
for f in /proc/irq/*/smp_affinity_list; do
  echo "$HK_CPUS" | sudo tee "$f" >/dev/null 2>&1 || true
done
echo "$HK_CPUS" | sudo tee /proc/irq/default_smp_affinity >/dev/null 2>&1 || true

echo "== частота"
if ls /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor >/dev/null 2>&1; then
  for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance | sudo tee "$g" >/dev/null; done
else
  echo "cpufreq нет (на Graviton частота фиксирована) — фиксируем в отчёте"
fi
[[ -f /sys/devices/system/cpu/cpufreq/boost ]] && echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost >/dev/null || true

echo "== systemd: делегирование cpu и linger (для H5)"
sudo mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo tee /etc/systemd/system/user@.service.d/delegate.conf >/dev/null
sudo systemctl daemon-reload
sudo loginctl enable-linger "$USER"
sudo systemctl restart "user@$(id -u).service" || true
export XDG_RUNTIME_DIR=/run/user/$(id -u)
if systemd-run --user --scope --quiet -p CPUQuota=150% true; then
  echo "systemd-run --user с CPUQuota работает"
else
  echo "ВНИМАНИЕ: systemd-run --user не работает — серия H5 (cpu_limit) упадёт; остальные прогоны не затронуты"
fi

echo "== сборка и тесты"
go build -o bin/ ./cmd/...
go test ./internal/... >/dev/null && echo "go test: ok"
python3 sim/mgk.py --selftest >/dev/null && echo "mgk selftest: ok"

echo "== окружение"
FORCE=1 bash run/env_check.sh
echo "cmdline: $(cat /proc/cmdline)"
echo
echo "Готово. Дальше: tmux new -s bench 'mkdir -p results && bash deploy/run_all.sh 2>&1 | tee -a results/run_all.log'"
