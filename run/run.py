#!/usr/bin/env python3
"""Прогон плана экспериментов: одна строка plan.csv — один прогон.

Для каждого прогона:
  1. запустить server (GOMAXPROCS, GOGC, taskset, CPU-лимит cgroup);
  2. дождаться READY, запустить loadgen;
  3. через warmup после START послать серверу SIGUSR1 (начало окна);
  4. после завершения loadgen послать SIGTERM (конец окна, final.json);
  5. сохранить всё в results/<run_id>/ вместе с meta.json.

Порядок строк перемешивается (дрейф машины не коррелирует с факторами).
Готовые прогоны пропускаются, поэтому прерванную серию можно продолжить.

  python3 run/make_plan.py --pilot > plan-pilot.csv
  python3 run/run.py --plan plan-pilot.csv --out results/pilot \\
      --server-cpus 0-3 --loadgen-cpus 4-7
"""
import argparse
import csv
import hashlib
import json
import os
import platform
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))
from mgk import parse_dist  # noqa: E402  (то же разбиение спецификаций, что в Go)

FACTORS = ["rho", "beta", "q", "P", "lc_dist", "alloc_lc", "alloc_be",
           "cpu_limit", "be_yield", "lc_source", "gogc"]


# Необязательные столбцы плана: если их нет или они пусты, run_id не меняется,
# поэтому уже сделанные прогоны старых планов не пересчитываются.
OPTIONAL = ["lg_mode"]  # lg_mode: "" или open — открытый цикл, closed — закрытый (статья 2)


CODE_SHA = ""


def run_id(row):
    key = ",".join(f"{k}={row[k]}" for k in FACTORS)
    key += "".join(f",{k}={row[k]}" for k in OPTIONAL if row.get(k))
    h = hashlib.sha1(key.encode()).hexdigest()[:8]
    return f"{h}_r{row['rep']}"


def derived(row, overhead_us=0.0):
    """Интенсивности из факторов: λ_LC = (1-β)ρP/(E[S_LC]+o), ρ_BE = βρ.

    o — накладные расходы CPU на один LC-запрос сверх E[S] (сокет, горутина,
    разбор кадра). Пилот показал o ≈ 13–23 мкс: без поправки номинальная ρ
    занижена. o меряется run/overhead.py на той же машине (--lc-overhead-us).
    """
    rho, beta, P = float(row["rho"]), float(row["beta"]), int(row["P"])
    _, mean_us = parse_dist(row["lc_dist"])
    return {
        "lc_rate": (1 - beta) * rho * P / ((mean_us + overhead_us) * 1e-6),
        "be_util": beta * rho,
        "lc_overhead_us": overhead_us,
    }


SOURCES = ["go.mod", "cmd", "internal", "run", "sim", "analysis"]


def code_sha256():
    """Отпечаток исходников (git на машине может не быть): sha256 по файлам *.go, *.py, *.sh, go.mod."""
    h = hashlib.sha256()
    files = []
    for s in SOURCES:
        p = ROOT / s
        files += [p] if p.is_file() else [f for f in p.rglob("*") if f.suffix in (".go", ".py", ".sh")]
    for f in sorted(files):
        if "__pycache__" in f.parts:
            continue
        h.update(str(f.relative_to(ROOT)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:16]


def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        return ""


def file_sha256(path):
    """Хеш собранного сервера: идентифицирует код, даже если git недоступен."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else ""


def wait_line(proc, word, timeout):
    """Ждёт строку word в stdout процесса (в отдельном потоке, с таймаутом)."""
    ev = threading.Event()

    def reader():
        for line in proc.stdout:
            if line.strip() == word:
                ev.set()
                return

    threading.Thread(target=reader, daemon=True).start()
    if not ev.wait(timeout):
        raise RuntimeError(f"не дождались {word}")


def prefix(cpus, cpu_limit):
    p = []
    if cpu_limit:
        if platform.system() != "Linux":
            raise SystemExit("CPU-лимит (cpu_limit) поддерживается только на Linux")
        pct = int(float(cpu_limit) * 100)
        # --user требует пользовательского systemd: deploy/setup.sh включает linger
        # и делегирует контроллер cpu пользовательским срезам.
        p += ["systemd-run", "--user", "--scope", "--quiet", "-p", f"CPUQuota={pct}%"]
    if cpus and platform.system() == "Linux":
        p += ["taskset", "-c", cpus]
    return p


def one_run(row, a, out, env_report):
    d = derived(row, a.lc_overhead_us)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GOMAXPROCS=str(row["P"]))
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")  # для systemd-run --user из tmux/nohup
    if row["gogc"]:
        env["GOGC"] = row["gogc"]
    seed = str(1000 + int(row["rep"]))

    srv = [str(ROOT / "bin/server"), "-addr", a.listen, "-out", str(out / "server"),
           "-be-util", f"{d['be_util']:.6f}", "-be-q", row["q"], "-be-alloc", row["alloc_be"] or "0",
           "-seed", seed]
    if row["be_yield"]:
        srv += ["-be-yield", row["be_yield"]]
    if a.trace:
        srv += ["-trace", a.trace]
    inproc = row["lc_source"] == "inproc"
    if inproc:
        srv += ["-lc-source", "inproc", "-lc-rate", f"{d['lc_rate']:.3f}",
                "-lc-dist", row["lc_dist"], "-lc-alloc", row["alloc_lc"] or "0"]
    srv = prefix(a.server_cpus, row["cpu_limit"]) + srv

    lg = [str(ROOT / "bin/loadgen"), "-addr", a.target, "-rate", f"{d['lc_rate']:.3f}",
          "-dist", row["lc_dist"], "-alloc", row["alloc_lc"] or "0",
          "-warmup", f"{a.warmup}s", "-dur", f"{a.dur}s", "-conns", str(a.conns),
          "-seed", seed, "-spin-ahead", a.spin_ahead, "-out", str(out / "loadgen")]
    if row.get("lg_mode"):
        lg += ["-mode", row["lg_mode"]]
    if a.raw:
        lg += ["-raw"]
    lg = a.loadgen_prefix.split() + prefix(a.loadgen_cpus, "") + lg

    meta = {"run_id": out.name, "factors": row, "derived": d, "server_cmd": srv,
            "loadgen_cmd": None if inproc else lg, "warmup_s": a.warmup, "dur_s": a.dur,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "env": env_report,
            "git": git_rev(),
            "code_sha256": CODE_SHA,
            "server_sha256": file_sha256(ROOT / "bin/server"),
            "status": "running"}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    with open(out / "server.log", "w") as slog, open(out / "loadgen.log", "w") as llog:
        sp = subprocess.Popen(srv, env=env, stdout=subprocess.PIPE, stderr=slog, text=True)
        try:
            wait_line(sp, "READY", 30)
            if inproc:
                time.sleep(a.warmup)
                sp.send_signal(signal.SIGUSR1)
                time.sleep(a.dur)
            else:
                lp = subprocess.Popen(lg, stdout=subprocess.PIPE, stderr=llog, text=True)
                wait_line(lp, "START", 120)
                time.sleep(a.warmup)
                sp.send_signal(signal.SIGUSR1)
                lp.wait(timeout=a.dur + 60)
                meta["loadgen_exit"] = lp.returncode
            sp.send_signal(signal.SIGTERM)
            sp.wait(timeout=30)
            meta["server_exit"] = sp.returncode
            meta["status"] = "done"
        finally:
            if sp.poll() is None:
                sp.kill()
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", default="results/main")
    ap.add_argument("--listen", default=":7070", help="адрес, который слушает server")
    ap.add_argument("--target", default="127.0.0.1:7070", help="адрес сервера для loadgen")
    ap.add_argument("--server-cpus", default="", help="taskset для сервера, напр. 0-3")
    ap.add_argument("--loadgen-cpus", default="", help="taskset для генератора, напр. 4-7")
    ap.add_argument("--loadgen-prefix", default="", help="напр. 'ssh loadgen-host' для второй машины")
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--dur", type=int, default=30)
    ap.add_argument("--conns", type=int, default=32)
    ap.add_argument("--spin-ahead", default="100us",
                    help="сколько ждать отправки в цикле, а не во сне (в VM нужно ~2ms: sleep просыпается поздно)")
    ap.add_argument("--trace", default="", help="длительность runtime/trace в окне, напр. 2s")
    ap.add_argument("--seed", type=int, default=42, help="seed перемешивания порядка")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="выполнить не больше N прогонов и выйти (0 — все)")
    ap.add_argument("--lc-overhead-us", type=float, default=0.0,
                    help="CPU на LC-запрос сверх E[S], мкс (из run/overhead.py); 0 — номинальная ρ")
    ap.add_argument("--raw", action="store_true", help="loadgen пишет все задержки окна (статья 2)")
    a = ap.parse_args()

    if not a.no_build:
        subprocess.run(["go", "build", "-o", "bin/", "./cmd/..."], cwd=ROOT, check=True)
    env_report = subprocess.run(["bash", str(ROOT / "run/env_check.sh")], cwd=ROOT,
                                capture_output=True, text=True, env=dict(os.environ, FORCE="1")).stdout

    global CODE_SHA
    CODE_SHA = code_sha256()
    rows = list(csv.DictReader(open(a.plan)))
    random.Random(a.seed).shuffle(rows)
    base = Path(a.out)
    todo = []
    for r in rows:
        out = base / run_id(r)
        m = out / "meta.json"
        if m.exists() and json.loads(m.read_text()).get("status") == "done":
            continue
        todo.append((r, out))
    if a.limit:
        todo = todo[:a.limit]
    per = a.warmup + a.dur + 5
    print(f"прогонов: {len(rows)}, осталось {len(todo)}, ~{len(todo) * per / 3600:.1f} ч", flush=True)

    for i, (r, out) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {out.name} " + " ".join(f"{k}={r[k]}" for k in FACTORS + OPTIONAL if r.get(k)), flush=True)
        try:
            one_run(r, a, out, env_report)
        except Exception as e:  # прогон помечается failed, серия продолжается
            print(f"  ОШИБКА: {e}", flush=True)
            m = out / "meta.json"
            meta = json.loads(m.read_text()) if m.exists() else {}
            meta.update(status="failed", error=str(e))
            m.write_text(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
