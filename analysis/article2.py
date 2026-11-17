#!/usr/bin/env python3
"""Анализ для статьи 2 по сериям ci и co (нужны сырые задержки, run.py --raw).

  python3 analysis/article2.py results-aws            # читает <dir>/ci и <dir>/co, пишет <dir>/article2.json

Нужны numpy и scipy (python3 -m venv .venv && .venv/bin/pip install numpy scipy).

Что считается:
  1. квантили прогона — оценка Харрелла—Дэвиса (HD);
  2. разложение разброса P99: между прогонами и внутри прогона;
  3. доверительные интервалы для среднего по прогонам P99: «наивный» (все запросы
     считаются независимыми) и двухуровневый бутстрэп (прогоны, затем запросы);
     покрытие и ширина в зависимости от числа прогонов m;
  4. сравнение конфигураций A и B: разность P99 и функция сдвига (shift function);
  5. открытый и закрытый цикл (coordinated omission).

Второй уровень бутстрэпа не перебирает запросы: p-квантиль повторной выборки из n
значений — это порядковая статистика с номером K, K − 1 ~ Bin(n, p); её и берём.
"""
import glob
import json
import os
import sys

import numpy as np
from scipy.stats import beta as beta_dist

RNG = np.random.default_rng(1)
B = 2000            # повторов бутстрэпа
TRIALS = 400        # случайных подвыборок прогонов при оценке покрытия
MS = [3, 5, 10, 20]
QS = [0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 0.999]


def hd(xs, p):
    """Оценка Харрелла—Дэвиса p-квантили по отсортированной выборке."""
    n = len(xs)
    a, b = p * (n + 1), (1 - p) * (n + 1)
    # веса заметно отличны от нуля только около n·p — берём окно ±8σ
    sd = (p * (1 - p) / n) ** 0.5
    lo, hi = max(0, int((p - 8 * sd) * n) - 2), min(n, int((p + 8 * sd) * n) + 2)
    w = np.diff(beta_dist.cdf(np.arange(lo, hi + 1) / n, a, b))
    return float(np.dot(w, xs[lo:hi]) / w.sum())


def load(series, fname="raw_latency.bin"):
    """{ключ конфигурации: [отсортированные задержки прогона, ...]}"""
    out = {}
    for m in sorted(glob.glob(os.path.join(series, "*", "meta.json"))):
        d = os.path.dirname(m)
        f = json.load(open(m))["factors"]
        if not json.load(open(os.path.join(d, "loadgen", "summary.json")))["valid"]:
            continue
        raw = os.path.join(d, "loadgen", fname)
        if not os.path.exists(raw):
            continue
        xs = np.sort(np.fromfile(raw, dtype="<i8")).astype(np.float32)
        out.setdefault((f["rho"], f["q"], f.get("lg_mode") or "open"), []).append(xs)
    return out


def resampled_quantile(xs, p, size):
    """Квантиль повторной выборки запросов одного прогона (см. заголовок файла)."""
    k = RNG.binomial(len(xs), p, size=size)
    return xs[np.clip(k, 0, len(xs) - 1)]


def ci_two_level(runs, p, b=B):
    """Двухуровневый бутстрэп среднего по прогонам p-квантили: прогоны, затем запросы."""
    m = len(runs)
    stats = np.empty(b)
    for i in range(b):
        pick = RNG.integers(0, m, m)
        stats[i] = np.mean([resampled_quantile(runs[j], p, 1)[0] for j in pick])
    return np.percentile(stats, [2.5, 97.5])


def ci_naive(runs, p, b=B):
    """Наивный интервал: запросы всех прогонов независимы, прогоны не пересэмплируются."""
    stats = np.mean([resampled_quantile(r, p, b) for r in runs], axis=0)
    return np.percentile(stats, [2.5, 97.5])


def diff_ci(a, b_, p, b=B):
    """Интервал для разности средних по прогонам квантилей B − A (двухуровневый)."""
    stats = np.empty(b)
    for i in range(b):
        pa, pb = RNG.integers(0, len(a), len(a)), RNG.integers(0, len(b_), len(b_))
        stats[i] = (np.mean([resampled_quantile(b_[j], p, 1)[0] for j in pb])
                    - np.mean([resampled_quantile(a[j], p, 1)[0] for j in pa]))
    return np.percentile(stats, [2.5, 97.5])


def us(x):
    return round(float(x) / 1e3, 1)


def analyse_ci(series):
    data = load(series)
    res = {}
    for key, runs in sorted(data.items()):
        name = f"rho={key[0]} q={key[1]}"
        p99 = np.array([hd(r, 0.99) for r in runs])
        truth = p99.mean()
        within = np.mean([resampled_quantile(r, 0.99, 500).std() for r in runs])
        r = dict(runs=len(runs), n_per_run=int(np.mean([len(x) for x in runs])),
                 p99_mean_us=us(truth), sd_between_us=us(p99.std(ddof=1)), sd_within_iid_us=us(within),
                 p99_min_us=us(p99.min()), p99_max_us=us(p99.max()), by_m={})
        for m in MS:
            cov = {"naive": 0, "two_level": 0}
            width = {"naive": [], "two_level": []}
            for _ in range(TRIALS):
                sub = [runs[j] for j in RNG.choice(len(runs), m, replace=False)]
                for kind, fn in (("naive", ci_naive), ("two_level", ci_two_level)):
                    lo, hi = fn(sub, 0.99, 300)
                    cov[kind] += lo <= truth <= hi
                    width[kind].append((hi - lo) / truth)
            r["by_m"][m] = {k: dict(coverage=round(cov[k] / TRIALS, 3),
                                    rel_width=round(float(np.median(width[k])), 3)) for k in cov}
        lo, hi = ci_two_level(runs, 0.99)
        r["by_m"][len(runs)] = {"two_level": dict(rel_width=round(float((hi - lo) / truth), 3))}
        res[name] = r
    keys = sorted(data, key=lambda k: float(k[1].rstrip("ms")))
    if len(keys) == 2:
        a, b_ = data[keys[0]], data[keys[1]]
        shift = []
        for p in QS:
            d = np.mean([hd(x, p) for x in b_]) - np.mean([hd(x, p) for x in a])
            lo, hi = diff_ci(a, b_, p, 1000)
            shift.append(dict(p=p, diff_us=us(d), lo_us=us(lo), hi_us=us(hi)))
        res["shift_B_minus_A"] = dict(A=keys[0][1], B=keys[1][1], quantiles=shift)
        # сколько прогонов нужно, чтобы различить A и B по P99
        power = {}
        for m in MS:
            hit = 0
            for _ in range(TRIALS):
                sa = [a[j] for j in RNG.choice(len(a), m, replace=False)]
                sb = [b_[j] for j in RNG.choice(len(b_), m, replace=False)]
                lo, hi = diff_ci(sa, sb, 0.99, 300)
                hit += lo > 0 or hi < 0
            power[m] = round(hit / TRIALS, 3)
        res["power_P99_A_vs_B"] = power
    return res


def analyse_co(series):
    opened, closed = load(series), load(series, "raw_latency_intended.bin")
    res = {}
    for rho in sorted({k[0] for k in opened}):
        row = {}
        for label, runs in (("open", opened.get((rho, "20ms", "open"))),
                            ("closed", opened.get((rho, "20ms", "closed"))),
                            ("closed_intended", closed.get((rho, "20ms", "closed")))):
            if not runs:
                continue
            row[label] = {"runs": len(runs)}
            for p, nm in ((0.5, "p50"), (0.99, "p99"), (0.999, "p999")):
                lo, hi = ci_two_level(runs, p, 1000)
                row[label][nm + "_us"] = [us(np.mean([hd(x, p) for x in runs])), us(lo), us(hi)]
        res[f"rho={rho}"] = row
    return res


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "results"
    out = {}
    if os.path.isdir(os.path.join(base, "ci")):
        out["ci"] = analyse_ci(os.path.join(base, "ci"))
    if os.path.isdir(os.path.join(base, "co")):
        out["co"] = analyse_co(os.path.join(base, "co"))
    path = os.path.join(base, "article2.json")
    json.dump(out, open(path, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", path, file=sys.stderr)


if __name__ == "__main__":
    main()
