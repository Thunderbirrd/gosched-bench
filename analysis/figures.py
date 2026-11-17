#!/usr/bin/env python3
"""Рисунки к статьям 1 и 2 по собранным результатам.

Запуск:  python3 analysis/figures.py results-aws figures
Нужен matplotlib (единственная внешняя зависимость репозитория).
Входные файлы: main/points.csv, main/sim-main-5seeds.json,
article2.json, article2_session2.json.
"""
import csv
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "results-aws")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "figures")
OUT.mkdir(exist_ok=True)

# Три цвета различимы при нарушениях цветового зрения; серии дополнительно
# различаются маркером и штрихом, чтобы рисунок читался в чёрно-белой печати.
COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
MARKERS = ["o", "s", "^"]
GRAY = "#52514e"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "axes.spines.top": False,
    "axes.spines.right": False, "axes.grid": True, "grid.color": "#e4e3df",
    "grid.linewidth": 0.6, "lines.linewidth": 1.6, "lines.markersize": 5,
    "legend.frameon": False, "savefig.dpi": 200, "savefig.bbox": "tight",
})


def ms(q):                       # "0.1ms" -> 0.1
    return float(q.replace("ms", ""))


def num(x):                      # 0.5 -> "0,5"
    return f"{x:g}".replace(".", ",")


points = list(csv.DictReader(open(SRC / "main/points.csv")))
sim = json.load(open(SRC / "main/sim-main-5seeds.json"))


def point_rows(rho, beta, P):
    rows = [r for r in points if r["rho"] == rho and r["beta"] == beta and r["P"] == P]
    return sorted(rows, key=lambda r: ms(r["q"]))


def sim_rows(rho, beta, P):
    rows = [r for r in sim if r["rho"] == rho and r["beta"] == beta and r["P"] == P]
    return sorted(rows, key=lambda r: ms(r["q"]))


def fig1():
    """P99 и P99,9 от q при rho=0,7, beta=0,5; эталоны для P=4."""
    fig, (a, b) = plt.subplots(1, 2, figsize=(7.2, 3.2), sharex=True)
    for i, P in enumerate(["2", "4", "8"]):
        rows = point_rows("0.7", "0.5", P)
        q = [ms(r["q"]) for r in rows]
        p99 = [float(r["p99_mean_ns"]) / 1e6 for r in rows]
        lo = [float(r["p99_ci_lo_ns"]) / 1e6 for r in rows]
        hi = [float(r["p99_ci_hi_ns"]) / 1e6 for r in rows]
        a.fill_between(q, lo, hi, color=COLORS[i], alpha=0.2, linewidth=0)
        a.plot(q, p99, color=COLORS[i], marker=MARKERS[i], label=f"P = {P}")
        b.plot(q, [float(r["p999_mean_ns"]) / 1e6 for r in rows],
               color=COLORS[i], marker=MARKERS[i], label=f"P = {P}")
    rows = sim_rows("0.7", "0.5", "4")
    q = [ms(r["q"]) for r in rows]
    for key, style, name in [("fcfs", "--", "FCFS"), ("rr", "-.", "круговое, 10 мс"),
                             ("prio", ":", "вытесняющий приоритет")]:
        a.plot(q, [r[key] / 1e6 for r in rows], color=GRAY, linestyle=style,
               linewidth=1.2, label=f"эталон: {name}")
    for ax, title in [(a, "а) P99"), (b, "б) P99,9")]:
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.axvline(10, color=GRAY, linewidth=0.8)
        ax.set_xticks([0.1, 1, 5, 10, 20, 50], ["0,1", "1", "5", "10", "20", "50"])
        ax.minorticks_off()
        ax.set_xlabel("q, мс"); ax.set_title(title, loc="left", fontsize=9)
        ax.set_yticks([1, 10, 100], ["1", "10", "100"])
    a.set_ylabel("время отклика, мс")
    fig.legend(*a.get_legend_handles_labels(), fontsize=7.5, ncol=3,
               loc="upper center", bbox_to_anchor=(0.5, 0.0))
    fig.savefig(OUT / "a1_fig1_p99_vs_q.png"); plt.close(fig)


def fig2():
    """Рост P99 относительно работы без фоновой нагрузки, q = 10 мс."""
    rhos, betas = ["0.3", "0.5", "0.7", "0.85"], ["0.25", "0.5", "0.75"]
    fig, axes = plt.subplots(1, 3, figsize=(7.2, 2.5), sharey=True)
    norm = LogNorm(1, 300)
    for ax, P in zip(axes, ["2", "4", "8"]):
        grid = [[next(float(r["slowdown"]) for r in points
                      if (r["rho"], r["beta"], r["P"], r["q"]) == (rho, beta, P, "10ms"))
                 for rho in rhos] for beta in betas]
        im = ax.imshow(grid, cmap="Blues", norm=norm, origin="lower", aspect="auto")
        for y, line in enumerate(grid):
            for x, v in enumerate(line):
                text = f"{v:.1f}".replace(".", ",") if v < 10 else f"{v:.0f}"
                ax.text(x, y, text, ha="center", va="center", fontsize=8,
                        color="white" if v > 20 else "#0b0b0b")
        ax.set_xticks(range(4), [num(float(r)) for r in rhos])
        ax.set_yticks(range(3), [num(float(b)) for b in betas])
        ax.set_xlabel("ρ"); ax.set_title(f"P = {P}", fontsize=9); ax.grid(False)
        for s in ax.spines.values():
            s.set_visible(False)
    axes[0].set_ylabel("β")
    fig.colorbar(im, ax=axes, label="отношение P99", shrink=0.9, pad=0.02)
    fig.savefig(OUT / "a1_fig2_slowdown.png"); plt.close(fig)


def fig3():
    """Индекс D от q при P = 4, beta = 0,5.

    Уровень rho = 0,3 не показан: при нём эталоны FCFS и вытесняющего
    приоритета почти совпадают и индекс неинформативен.
    """
    fig, ax = plt.subplots(figsize=(4.6, 3.0))
    styles = [(COLORS[0], "o", "-"), (COLORS[1], "s", "--"), (COLORS[2], "^", "-.")]
    for (color, marker, line), rho in zip(styles, ["0.5", "0.7", "0.85"]):
        rows = [r for r in sim_rows(rho, "0.5", "4") if ms(r["q"]) >= 1]
        d = [(r["meas"] - r["prio"]) / (r["fcfs"] - r["prio"]) for r in rows]
        ax.plot([ms(r["q"]) for r in rows], d, color=color, marker=marker,
                linestyle=line, label=f"ρ = {num(float(rho))}")
    ax.axhline(1, color=GRAY, linewidth=0.8); ax.axhline(0, color=GRAY, linewidth=0.8)
    ax.text(1, 1.02, "без приоритетов (FCFS)", fontsize=7.5, color=GRAY, va="bottom")
    ax.text(1, -0.03, "вытесняющий приоритет", fontsize=7.5, color=GRAY, va="top")
    ax.set_xscale("log")
    ax.set_xticks([1, 5, 10, 20, 50], ["1", "5", "10", "20", "50"]); ax.minorticks_off()
    ax.set_xlabel("q, мс"); ax.set_ylabel("индекс D"); ax.set_ylim(-0.12, 1.2)
    ax.set_yticks([0, 0.2, 0.4, 0.6, 0.8, 1.0], ["0", "0,2", "0,4", "0,6", "0,8", "1"])
    ax.legend(fontsize=7.5, loc="center left", bbox_to_anchor=(1.0, 0.5))
    fig.savefig(OUT / "a1_fig3_index_d.png"); plt.close(fig)


def fig_shift():
    """Функции сдвига для пар q = 5 и 10 мс, q = 5 и 6 мс (статья 2)."""
    pairs = [("а) q = 10 мс и q = 5 мс",
              json.load(open(SRC / "article2.json"))["ci"]["shift_B_minus_A"]["quantiles"]),
             ("б) q = 6 мс и q = 5 мс",
              json.load(open(SRC / "article2_session2.json"))["shift 6ms-5ms"])]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0), sharey=True)
    for ax, (title, qs) in zip(axes, pairs):
        x = range(len(qs))
        d = [v["diff_us"] for v in qs]
        err = [[v["diff_us"] - v["lo_us"] for v in qs], [v["hi_us"] - v["diff_us"] for v in qs]]
        ax.errorbar(x, d, yerr=err, color=COLORS[0], marker="o", capsize=3, linewidth=1.2)
        ax.axhline(0, color=GRAY, linewidth=0.8)
        ax.set_yscale("symlog", linthresh=1)
        ax.set_yticks([0, 1, 10, 100, 1000], ["0", "1", "10", "100", "1000"])
        ax.set_ylim(-0.5, 6000); ax.minorticks_off()
        ax.set_xticks(x, [num(v["p"]) for v in qs])
        ax.set_xlabel("уровень квантиля p"); ax.set_title(title, loc="left", fontsize=9)
    axes[0].set_ylabel("разность квантилей, мкс")
    fig.savefig(OUT / "a2_fig1_shift.png"); plt.close(fig)


fig1(); fig2(); fig3(); fig_shift()
print("готово:", sorted(p.name for p in OUT.glob("*.png")))
