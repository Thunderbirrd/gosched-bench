#!/usr/bin/env python3
"""Имитация эталонов для индекса D: многоканальная система M/G/k с двумя классами.

Классы:
  LC — пуассоновский поток, время обслуживания по распределению (как в loadgen);
  BE — пуассоновский поток, детерминированное время q (как в cmd/server).

Дисциплины (общая очередь на k обслуживающих приборов = GOMAXPROCS):
  fcfs — без приоритетов, обслуживание до конца (граница «без приоритетов»);
  rr   — круговое обслуживание с квантом (по умолчанию 10 мс, как forcePreemptNS);
  prio — вытесняющий приоритет LC над BE с дообслуживанием (идеал).

Выход — JSON с перцентилями задержки LC (нс), чтобы сравнивать с loadgen.

Примеры:
  python3 sim/mgk.py --P 4 --rho 0.7 --beta 0.5 --lc-dist exp:50us --be-q 20ms --disc all
  python3 sim/mgk.py --selftest
"""
import argparse
import collections
import json
import math
import random
import sys

INF = float("inf")


def parse_dur(s):
    """'50us', '1ms', '10ms', '0.1ms', '2s' -> микросекунды (float)."""
    for suf, mul in (("ns", 1e-3), ("us", 1.0), ("µs", 1.0), ("ms", 1e3), ("s", 1e6)):
        if s.endswith(suf):
            return float(s[: -len(suf)]) * mul
    raise ValueError(f"длительность {s!r}: нужен суффикс ns/us/ms/s")


def parse_dist(spec):
    """Те же спецификации, что в internal/dist: const:, exp:, bimodal:short,long,p.
    Возвращает (sampler(rng) -> мкс, mean_us)."""
    kind, _, arg = spec.partition(":")
    if kind == "const":
        d = parse_dur(arg)
        return (lambda r: d), d
    if kind == "exp":
        m = parse_dur(arg)
        return (lambda r: r.expovariate(1.0 / m)), m
    if kind == "bimodal":
        a, b, p = arg.split(",")
        s, l, p = parse_dur(a), parse_dur(b), float(p)
        return (lambda r: l if r.random() < p else s), (1 - p) * s + p * l
    raise ValueError(f"неизвестное распределение {spec!r}")


class Job:
    __slots__ = ("lc", "arr", "rem")

    def __init__(self, lc, arr, rem):
        self.lc, self.arr, self.rem = lc, arr, rem


def simulate(k, lam_lc, lc_sampler, lam_be, be_q, disc, quantum, n_lc, seed, warm_frac=0.1):
    """Возвращает отсортированный список задержек LC (мкс), без прогрева."""
    rng = random.Random(seed)
    # Приходы двух классов, слитые по времени.
    horizon = n_lc / lam_lc
    arrivals = []
    t = 0.0
    for _ in range(n_lc):
        t += rng.expovariate(lam_lc)
        arrivals.append((t, True, lc_sampler(rng)))
    if lam_be > 0:
        t = 0.0
        while True:
            t += rng.expovariate(lam_be)
            if t > horizon:
                break
            arrivals.append((t, False, be_q))
    arrivals.sort(key=lambda a: a[0])

    run = [None] * k            # задача на приборе
    slice_end = [INF] * k       # конец кванта (только rr)
    q_all = collections.deque()  # fcfs, rr
    q_lc = collections.deque()   # prio
    q_be = collections.deque()   # prio
    out = []
    warm_t = warm_frac * horizon

    def take():
        if disc == "prio":
            if q_lc:
                return q_lc.popleft()
            if q_be:
                return q_be.popleft()
            return None
        return q_all.popleft() if q_all else None

    def start(s, job, now):
        run[s] = job
        slice_end[s] = now + quantum if disc == "rr" else INF

    t = 0.0
    ai = 0
    while True:
        t_arr = arrivals[ai][0] if ai < len(arrivals) else INF
        t_srv, s_srv, done = INF, -1, False
        for s in range(k):
            j = run[s]
            if j is not None:
                tc = t + j.rem
                te = min(tc, slice_end[s])
                if te < t_srv:
                    t_srv, s_srv, done = te, s, tc <= slice_end[s]
        tn = min(t_arr, t_srv)
        if tn == INF:
            break
        dt = tn - t
        if dt > 0:
            for j in run:
                if j is not None:
                    j.rem -= dt
        t = tn

        if s_srv >= 0 and t_srv <= t_arr:
            j = run[s_srv]
            # Тип события берём из того, какое время было минимальным, а не из
            # остатка j.rem: при больших t остаток может «застрять» около нуля
            # из-за округления, и цикл бы не продвинулся.
            if done:  # завершение
                if j.lc and j.arr >= warm_t:
                    out.append(t - j.arr)
                run[s_srv] = None
                nxt = take()
                if nxt is not None:
                    start(s_srv, nxt, t)
            else:  # конец кванта (rr)
                if q_all:
                    q_all.append(j)
                    start(s_srv, q_all.popleft(), t)
                else:
                    slice_end[s_srv] = t + quantum
            continue

        _, lc, svc = arrivals[ai]
        ai += 1
        job = Job(lc, t, svc)
        idle = next((s for s in range(k) if run[s] is None), -1)
        if idle >= 0:
            start(idle, job, t)
        elif disc == "prio" and lc:
            victim = next((s for s in range(k) if not run[s].lc), -1)
            if victim >= 0:
                q_be.appendleft(run[victim])  # дообслуживание: вперёд очереди BE
                start(victim, job, t)
            else:
                q_lc.append(job)
        elif disc == "prio":
            q_be.append(job)
        else:
            q_all.append(job)

    out.sort()
    return out


def pct(xs, p):
    if not xs:
        return 0.0
    i = max(0, math.ceil(p * len(xs)) - 1)
    return xs[i]


def summary(xs):
    to_ns = lambda us: int(round(us * 1000))
    return {
        "count": len(xs),
        "mean_ns": to_ns(sum(xs) / len(xs)) if xs else 0,
        "p50_ns": to_ns(pct(xs, 0.5)),
        "p99_ns": to_ns(pct(xs, 0.99)),
        "p999_ns": to_ns(pct(xs, 0.999)),
    }


def run_point(P, rho, beta, lc_dist, be_q, disc, quantum, n, seed):
    sampler, mean_lc = parse_dist(lc_dist)
    q_us = parse_dur(be_q)
    lam_lc = (1 - beta) * rho * P / mean_lc  # в 1/мкс
    lam_be = beta * rho * P / q_us
    xs = simulate(P, lam_lc, sampler, lam_be, q_us, disc, parse_dur(quantum), n, seed)
    return summary(xs)


def selftest():
    ok = True

    def check(name, got, want, tol):
        nonlocal ok
        rel = abs(got - want) / want
        flag = "ok" if rel <= tol else "FAIL"
        ok &= rel <= tol
        print(f"{flag:4} {name}: {got:.1f} мкс, ожидалось {want:.1f} (откл. {rel:.1%})")

    n = 300_000
    # M/M/1: E[T] = 1/(mu - lambda); S=50 мкс, rho=0.5 -> 100 мкс
    s = run_point(1, 0.5, 0.0, "exp:50us", "1ms", "fcfs", "10ms", n, 1)
    check("M/M/1 FCFS, среднее", s["mean_ns"] / 1000, 100.0, 0.03)
    # M/D/1 (Поллачек–Хинчин): E[T] = S + rho*S/(2(1-rho)) = 50 + 25 = 75 мкс
    s = run_point(1, 0.5, 0.0, "const:50us", "1ms", "fcfs", "10ms", n, 2)
    check("M/D/1 FCFS, среднее", s["mean_ns"] / 1000, 75.0, 0.03)
    # Вытесняющий приоритет: LC не видит BE, значит как M/M/1 с rho_LC = 0.35 -> 50/(1-0.35)
    s = run_point(1, 0.7, 0.5, "exp:50us", "5ms", "prio", "10ms", n, 3)
    check("prio: LC как без BE, среднее", s["mean_ns"] / 1000, 50 / (1 - 0.35), 0.03)
    # RR с огромным квантом совпадает с FCFS
    a = run_point(2, 0.6, 0.3, "exp:50us", "1ms", "fcfs", "10ms", 100_000, 4)
    b = run_point(2, 0.6, 0.3, "exp:50us", "1ms", "rr", "1000s", 100_000, 4)
    check("rr(квант=∞) = fcfs, p99", b["p99_ns"] / 1000, a["p99_ns"] / 1000, 1e-9)
    print("SELFTEST", "OK" if ok else "FAILED")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--P", type=int, default=4, help="число приборов (GOMAXPROCS)")
    ap.add_argument("--rho", type=float, default=0.7, help="суммарная утилизация")
    ap.add_argument("--beta", type=float, default=0.5, help="доля BE в утилизации")
    ap.add_argument("--lc-dist", default="exp:50us")
    ap.add_argument("--be-q", default="20ms")
    ap.add_argument("--disc", default="all", choices=["fcfs", "rr", "prio", "all"])
    ap.add_argument("--quantum", default="10ms", help="квант rr")
    ap.add_argument("--n", type=int, default=200_000, help="число LC-запросов")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    discs = ["fcfs", "rr", "prio"] if a.disc == "all" else [a.disc]
    res = {"params": vars(a), "results": {}}
    for d in discs:
        res["results"][d] = run_point(a.P, a.rho, a.beta, a.lc_dist, a.be_q, d, a.quantum, a.n, a.seed)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
