#!/usr/bin/env python3
"""Строит план прогонов (CSV) — одна строка на прогон.

Уровни факторов задаются прямо здесь: править удобнее, чем передавать флаги.

  python3 run/make_plan.py            > plan-main.csv      # статья 1: полный факторный план
  python3 run/make_plan.py --ablation > plan-ablation.csv  # статья 1: абляции H1–H5
  python3 run/make_plan.py --h1       > plan-h1.csv        # статья 1: Gosched в разных режимах загрузки
  python3 run/make_plan.py --co       > plan-co.csv        # статья 2: открытый vs закрытый цикл
  python3 run/make_plan.py --ci       > plan-ci.csv        # статья 2: ширина интервалов от числа прогонов
  python3 run/make_plan.py --ci --reps 20 > plan-env.csv   # статья 2: то же для сравнения окружений
  python3 run/make_plan.py --pilot    > plan-pilot.csv     # пилот на 4 vCPU
"""
import csv
import itertools
import sys

# --- Статья 1, основная кампания (таблица «Факторы и уровни»). ---
RHO = [0.3, 0.5, 0.7, 0.85]
BETA = [0.0, 0.25, 0.5, 0.75]
Q = ["0.1ms", "1ms", "5ms", "10ms", "20ms", "50ms"]
P = [2, 4, 8]
LC_DIST = "exp:50us"
REPS = 5
# Точки «только LC»: β = 0 при ρ' = ρ(1−β). У них та же интенсивность LC, что у
# точки (ρ, β), но без фоновой работы — эмпирический идеал для индекса D.
LC_ONLY_REFS = True

# Пилот на 4 vCPU (сделан 06.11.2026).
PILOT = dict(RHO=[0.3, 0.7, 0.85], BETA=[0.0, 0.5], P=[2], REPS=2, LC_ONLY_REFS=False)

# --- Статья 1, абляции: базовые точки × варианты, в каждом нейтрализован один механизм. ---
ABL_BASE = dict(RHO=[0.5, 0.7], BETA=[0.5], Q=["5ms", "20ms"], P=[4], REPS=5)
# (имя, изменения относительно базовой точки)
ABL_VARIANTS = [
    ("base", {}),
    ("H1 Gosched 1ms", {"be_yield": "1ms"}),
    ("H1 Gosched 100us", {"be_yield": "100us"}),
    ("H3 без сети", {"lc_source": "inproc"}),
    # В пилоте 4 КиБ / 64 КиБ эффекта не дали — уровни подняты.
    ("H4 alloc", {"alloc_lc": 65536, "alloc_be": 1 << 20}),
    ("H4 alloc, GOGC=off", {"alloc_lc": 65536, "alloc_be": 1 << 20, "gogc": "off"}),
    ("H5 C=2 < P", {"cpu_limit": 2}),
    ("H5 C=4 = P", {"cpu_limit": 4}),
]

# H1 по режимам: в абляциях (BE занимает 1–1,4 из 4 P) Gosched ухудшил P99, в пилоте
# (почти насыщение) — улучшил. Проверяем знак эффекта при свободных и при занятых P.
H1_REGIMES = [(0.7, 0.5, 2), (0.85, 0.75, 2), (0.85, 0.75, 4)]  # (ρ, β, P)
H1 = dict(Q=["5ms", "20ms"], YIELD=["", "1ms"], REPS=5)

# --- Статья 2. ---
# Coordinated omission: одна конфигурация, открытый и закрытый цикл по одному
# расписанию (тот же seed у пары прогонов с одинаковым rep).
CO = dict(RHO=[0.3, 0.5, 0.7], BETA=[0.5], Q=["20ms"], P=[4], REPS=10)
# Сравнение двух близких конфигураций A и B (отличаются только q) с большим
# числом прогонов: по ним строится зависимость ширины доверительных интервалов
# от числа прогонов m (подвыборки 5, 10, 20, 50). Пишутся сырые задержки (--raw).
CI = dict(RHO=0.5, BETA=0.5, P=4, QS=["5ms", "10ms"], REPS=50)

COLS = ["rho", "beta", "q", "P", "lc_dist", "alloc_lc", "alloc_be",
        "cpu_limit", "be_yield", "lc_source", "gogc", "rep"]


def row(rho, beta, q, p, rep, **kw):
    r = dict(rho=rho, beta=beta, q=q, P=p, lc_dist=LC_DIST, alloc_lc=0, alloc_be=0,
             cpu_limit="", be_yield="", lc_source="net", gogc="", rep=rep)
    r.update(kw)
    return r


def main_plan(g):
    points = []
    for rho, beta, q, p in itertools.product(g["RHO"], g["BETA"], g["Q"], g["P"]):
        if beta == 0 and q != g["Q"][0]:  # при β = 0 гранулярность q не влияет
            continue
        points.append((rho, beta, q, p))
    if g["LC_ONLY_REFS"]:
        seen = {(rho, p) for rho, beta, q, p in points if beta == 0}
        for rho, beta, q, p in list(points):
            ref = (round(rho * (1 - beta), 4), p)
            if beta > 0 and ref not in seen:
                seen.add(ref)
                points.append((ref[0], 0.0, g["Q"][0], p))
    return [row(rho, beta, q, p, rep) for rho, beta, q, p in points for rep in range(g["REPS"])]


def ablation_plan():
    b = ABL_BASE
    rows = []
    for rho, beta, q, p in itertools.product(b["RHO"], b["BETA"], b["Q"], b["P"]):
        for _, kw in ABL_VARIANTS:
            rows += [row(rho, beta, q, p, rep, **kw) for rep in range(b["REPS"])]
    return rows


def h1_plan():
    return [row(rho, beta, q, p, rep, be_yield=y)
            for (rho, beta, p), q, y, rep in itertools.product(H1_REGIMES, H1["Q"], H1["YIELD"], range(H1["REPS"]))]


def co_plan():
    c = CO
    return [row(rho, beta, q, p, rep, lg_mode=m)
            for rho, beta, q, p, rep, m in itertools.product(c["RHO"], c["BETA"], c["Q"], c["P"],
                                                            range(c["REPS"]), ["open", "closed"])]


def ci_plan(reps, qs):
    c = CI
    return [row(c["RHO"], c["BETA"], q, c["P"], rep) for q in qs for rep in range(reps)]


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main():
    g = dict(globals())
    if "--ablation" in sys.argv:
        rows = ablation_plan()
    elif "--h1" in sys.argv:
        rows = h1_plan()
    elif "--co" in sys.argv:
        rows = co_plan()
    elif "--ci" in sys.argv:
        # --q 6ms: одна дополнительная конфигурация, близкая к A (для кривой мощности)
        rows = ci_plan(arg("--reps", CI["REPS"]), [arg("--q", "")] if "--q" in sys.argv else CI["QS"])
    else:
        if "--pilot" in sys.argv:
            g.update(PILOT)
        rows = main_plan(g)
    cols = COLS + (["lg_mode"] if any("lg_mode" in r for r in rows) else [])
    w = csv.DictWriter(sys.stdout, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    print(f"{len(rows)} прогонов", file=sys.stderr)


if __name__ == "__main__":
    main()
