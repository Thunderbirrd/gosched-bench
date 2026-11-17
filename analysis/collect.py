#!/usr/bin/env python3
"""Сводит результаты прогонов в две таблицы.

  results.csv — одна строка на прогон: факторы, перцентили R, валидность,
                задержка планирования, BE, GC, троттлинг;
  points.csv  — одна строка на точку плана (повторы свёрнуты): среднее P99/P99,9
                по повторам, 95% бутстрэп-интервал, фактическая загрузка,
                эталоны имитации, индекс D, а также slowdown и D_emp
                относительно точки «только LC».

  python3 analysis/collect.py results/pilot            # без имитации
  python3 analysis/collect.py results/pilot --sim      # + fcfs/rr/prio и D

Только стандартная библиотека Python.
"""
import argparse
import csv
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))
import mgk  # noqa: E402

FACTORS = ["rho", "beta", "q", "P", "lc_dist", "alloc_lc", "alloc_be",
           "cpu_limit", "be_yield", "lc_source", "gogc"]


def load_run(d):
    meta = json.loads((d / "meta.json").read_text())
    if meta.get("status") != "done":
        return None
    row = {k: meta["factors"][k] for k in FACTORS}
    row["lg_mode"] = meta["factors"].get("lg_mode") or "open"
    row["rep"] = meta["factors"]["rep"]
    row["run_id"] = meta["run_id"]
    fin = json.loads((d / "server/final.json").read_text())
    w = fin["window"]
    row.update(
        sched_p99_s=w["sched_latencies"]["p99_s"],
        sched_p999_s=w["sched_latencies"]["p999_s"],
        gc_cycles=w["scalars"].get("/gc/cycles/total:gc-cycles", 0),
        assist_cpu_s=w["scalars"].get("/cpu/classes/gc/mark/assist:cpu-seconds", 0),
        nr_throttled=w["nr_throttled"],
        be_achieved_util=fin["be"]["achieved_util"],
        # Фактическая загрузка сервера по getrusage: с ней сравнивается номинальная ρ.
        util_meas=round((w["scalars"]["rusage_user_s"] + w["scalars"]["rusage_sys_s"])
                        / (w["window_s"] * int(row["P"])), 4),
        lc_overhead_us=meta.get("derived", {}).get("lc_overhead_us", 0),
    )
    if row["lc_source"] == "inproc":
        s = fin["lc_inproc_total"]["summary"]
        row.update(valid=True, lost=0, lag_p999_ns=0)
    else:
        lg = json.loads((d / "loadgen/summary.json").read_text())
        s = lg["latency"]
        row.update(valid=lg["valid"], lost=lg["lost"], lag_p999_ns=lg["send_lag"]["p999_ns"],
                   handler_wait_p99_ns=lg["handler_wait"]["p99_ns"],
                   achieved_rate=lg.get("achieved_rate", ""))
        if "latency_intended" in lg:  # закрытый цикл: та же задержка, но от плана
            li = lg["latency_intended"]
            row.update(int_p50_ns=li["p50_ns"], int_p99_ns=li["p99_ns"], int_p999_ns=li["p999_ns"])
    row.update(n=s["count"], p50_ns=s["p50_ns"], p99_ns=s["p99_ns"], p999_ns=s["p999_ns"], max_ns=s["max_ns"])
    return row


def boot_ci(xs, b=2000, seed=1):
    """95% бутстрэп-интервал среднего по повторам."""
    if len(xs) < 2:
        return xs[0], xs[0]
    r = random.Random(seed)
    means = sorted(statistics.fmean(r.choices(xs, k=len(xs))) for _ in range(b))
    return means[int(0.025 * b)], means[int(0.975 * b) - 1]


def sim_dist(spec, overhead_us):
    """Эффективное время обслуживания для имитации: E[S] + накладные (для exp — сдвиг среднего)."""
    if not overhead_us:
        return spec
    kind, _, val = spec.partition(":")
    if kind != "exp":
        return spec  # для const/bimodal поправка не применяется
    _, mean_us = mgk.parse_dist(spec)
    return f"exp:{mean_us + overhead_us:g}us"


def add_empirical_d(points):
    """slowdown и D_emp: идеал — точка «только LC» (β = 0, ρ' = ρ(1−β), то же P).

    slowdown = P99 / P99 идеала — чисто эмпирический показатель.
    D_emp — индекс (3) с измеренным идеалом и FCFS из имитации (если FCFS > идеала).

    Такой идеал включает все накладные стенда и шум окружения, поэтому D_emp
    не зависит от того, насколько имитация похожа на реальную машину.
    """
    ref = {(p["rho"], p["P"], p["lg_mode"]): p["p99_mean_ns"] for p in points
           if float(p["beta"]) == 0 and not p["be_yield"] and p["lc_source"] == "net"
           and not p["cpu_limit"] and p["alloc_lc"] in ("0", "") and not p["gogc"]}
    for p in points:
        beta = float(p["beta"])
        if beta == 0:
            continue
        key = (f"{round(float(p['rho']) * (1 - beta), 4):g}", p["P"], p["lg_mode"])
        ideal = ref.get(key)
        if ideal is None:
            continue
        p["p99_lc_only_ns"] = ideal
        p["slowdown"] = round(p["p99_mean_ns"] / ideal, 3)  # во сколько раз BE увеличила P99
        fcfs = p.get("sim_fcfs_p99_ns")
        if fcfs and fcfs > ideal:
            p["D_emp"] = round((p["p99_mean_ns"] - ideal) / (fcfs - ideal), 4)


def write_csv(path, rows):
    if not rows:
        return
    cols = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results")
    ap.add_argument("--sim", action="store_true", help="посчитать эталоны M/G/k и индекс D")
    ap.add_argument("--sim-n", type=int, default=200_000)
    ap.add_argument("--include-invalid", action="store_true", help="не отбрасывать невалидные прогоны (только для отладки)")
    ap.add_argument("--overhead-us", type=float, default=None,
                    help="накладные на LC-запрос для имитации; по умолчанию — из meta.json прогонов")
    a = ap.parse_args()
    base = Path(a.results)

    runs = [r for d in sorted(base.iterdir()) if (d / "meta.json").exists() for r in [load_run(d)] if r]
    write_csv(base / "results.csv", runs)

    groups = defaultdict(list)
    for r in runs:
        if r["valid"] or a.include_invalid:
            groups[tuple(r[k] for k in FACTORS + ["lg_mode"])].append(r)
    points = []
    for key, rs in sorted(groups.items()):
        pt = dict(zip(FACTORS + ["lg_mode"], key))
        p99 = [r["p99_ns"] for r in rs]
        p999 = [r["p999_ns"] for r in rs]
        lo, hi = boot_ci(p99)
        pt.update(reps=len(rs), p99_mean_ns=round(statistics.fmean(p99)), p99_ci_lo_ns=round(lo),
                  p99_ci_hi_ns=round(hi), p999_mean_ns=round(statistics.fmean(p999)))
        pt["util_meas"] = round(statistics.fmean(r["util_meas"] for r in rs), 4)
        if a.sim:
            dist = sim_dist(pt["lc_dist"], a.overhead_us if a.overhead_us is not None else rs[0]["lc_overhead_us"])
            for disc in ("fcfs", "rr", "prio"):
                s = mgk.run_point(int(pt["P"]), float(pt["rho"]), float(pt["beta"]), dist,
                                  pt["q"], disc, "10ms", a.sim_n, 1)
                pt[f"sim_{disc}_p99_ns"] = s["p99_ns"]
            ideal, fcfs = pt["sim_prio_p99_ns"], pt["sim_fcfs_p99_ns"]
            pt["D"] = round((pt["p99_mean_ns"] - ideal) / (fcfs - ideal), 4) if fcfs > ideal else ""
        points.append(pt)
    add_empirical_d(points)
    write_csv(base / "points.csv", points)

    bad = sum(1 for r in runs if not r["valid"])
    print(f"прогонов: {len(runs)} (невалидных {bad}); точек: {len(points)}")
    print(f"-> {base / 'results.csv'}\n-> {base / 'points.csv'}")


if __name__ == "__main__":
    main()
