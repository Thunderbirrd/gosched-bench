#!/usr/bin/env python3
"""Замер накладных расходов CPU на один LC-запрос (поправка к ρ).

Пилот показал: кроме E[S] каждый LC-запрос тратит ещё ~13–23 мкс CPU
(чтение/запись сокета, создание горутины, разбор кадра). Без поправки
номинальная ρ занижена: 0,85 по плану давала полную загрузку.

Скрипт делает короткие прогоны без BE (β = 0) и считает
    o = CPU-время сервера за окно / число ответов в окне − E[S].
Результат — медиана по точкам — пишется в <out>/overhead.json; его
передают в run.py как --lc-overhead-us.

  python3 run/overhead.py --out results/overhead --server-cpus 2-10 --loadgen-cpus 16-31
"""
import argparse
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))
from mgk import parse_dist  # noqa: E402

COLS = ["rho", "beta", "q", "P", "lc_dist", "alloc_lc", "alloc_be",
        "cpu_limit", "be_yield", "lc_source", "gogc", "rep"]
POINTS = [(0.3, 2), (0.5, 2), (0.5, 4), (0.5, 8)]  # (ρ номинальная, P)
LC_DIST = "exp:50us"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="results/overhead")
    ap.add_argument("--server-cpus", default="")
    ap.add_argument("--loadgen-cpus", default="")
    ap.add_argument("--spin-ahead", default="100us")
    ap.add_argument("--dur", type=int, default=15)
    ap.add_argument("--reps", type=int, default=2)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    plan = out / "plan.csv"
    with open(plan, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for rho, p in POINTS:
            for rep in range(a.reps):
                w.writerow([rho, 0.0, "0.1ms", p, LC_DIST, 0, 0, "", "", "net", "", rep])

    subprocess.run([sys.executable, str(ROOT / "run/run.py"), "--plan", str(plan), "--out", str(out),
                    "--server-cpus", a.server_cpus, "--loadgen-cpus", a.loadgen_cpus,
                    "--spin-ahead", a.spin_ahead, "--warmup", "3", "--dur", str(a.dur), "--no-build"],
                   check=True)

    _, mean_us = parse_dist(LC_DIST)
    est = []
    for m in sorted(out.glob("*/meta.json")):
        meta = json.loads(m.read_text())
        if meta.get("status") != "done":
            continue
        d = m.parent
        sc = json.loads((d / "server/final.json").read_text())["window"]["scalars"]
        n = json.loads((d / "loadgen/summary.json").read_text())["latency"]["count"]
        if not n:
            continue
        cpu_us = (sc["rusage_user_s"] + sc["rusage_sys_s"]) * 1e6
        o = cpu_us / n - mean_us
        f = meta["factors"]
        est.append({"rho": f["rho"], "P": f["P"], "rep": f["rep"], "cpu_per_req_us": round(cpu_us / n, 2),
                    "overhead_us": round(o, 2)})
        print(f"ρ={f['rho']} P={f['P']} rep={f['rep']}: CPU на запрос {cpu_us / n:.1f} мкс, накладные {o:.1f} мкс")
    if not est:
        sys.exit("нет готовых прогонов")
    med = round(statistics.median(e["overhead_us"] for e in est), 1)
    (out / "overhead.json").write_text(json.dumps({"overhead_us": med, "points": est}, ensure_ascii=False, indent=2))
    print(f"медиана накладных: {med} мкс -> {out / 'overhead.json'}")


if __name__ == "__main__":
    main()
