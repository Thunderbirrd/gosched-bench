# Разбор трасс планировщика серии h1trace: кто будит горутины, ждавшие сеть (работающий P, простаивающий поток, sysmon).
# Запуск из каталога с h1trace/: python3 analysis/h1trace.py  (нужен go в PATH)
import re,subprocess,json,glob,collections as C,statistics as st
rx=re.compile(r'M=(-?\d+) P=(-?\d+) G=(-?\d+) StateTransition Time=(\d+) GoID=(\d+) (\w+)->(\w+) Reason="([^"]*)"')
rm=re.compile(r'M=(-?\d+) P=(-?\d+) ')
def analyse(path):
    out=subprocess.run(['go','tool','trace','-d=parsed',path],capture_output=True,text=True).stdout.split('\n')
    hasP=C.Counter(); noP=C.Counter()
    for l in out:
        if l.startswith('M='):
            m=rm.match(l)
            if m: (hasP if m.group(2)!='-1' else noP)[m.group(1)]+=1
    sysmon={m for m in noP if m not in hasP and m!='-1'}
    wait={}; runnable={}; n=C.Counter(); delay=C.defaultdict(list); bursts=[]
    for l in out:
        m=rx.match(l)
        if not m: continue
        M,P,G,T,gid,a,b,reason=m.groups(); T=int(T)
        if b=='Waiting': wait[gid]=reason
        elif a=='Waiting' and b=='Runnable' and wait.get(gid)=='network':
            how='sysmon' if M in sysmon else ('idleM' if P=='-1' else 'P')
            n[how]+=1; runnable[gid]=(T,how)
            if how=='sysmon' and (not bursts or T-bursts[-1]>1e6): bursts.append(T)
        elif a=='Runnable' and b=='Running' and gid in runnable:
            t,how=runnable.pop(gid); delay[how].append((T-t)/1e6)
    gaps=[(b-a)/1e6 for a,b in zip(bursts,bursts[1:])]
    return n,delay,gaps,len(sysmon)
rows=[]
for d in sorted(glob.glob('h1trace/*/')):
    F=json.load(open(d+'meta.json'))['factors']; rows.append(((F['rho'],F['P'],F['q'],F['be_yield'] or '-'),)+analyse(d+'server/trace.out'))
for k,n,delay,gaps,ns in sorted(rows):
    tot=sum(n.values())
    q=lambda x,p: sorted(x)[int(p*(len(x)-1))] if x else float('nan')
    print(*k,f"| пробуждений {tot}: работающим P {n['P']/tot:.0%}, простаивающим потоком {n['idleM']/tot:.0%}, sysmon {n['sysmon']/tot:.0%} | пачек sysmon {len(gaps)+1 if gaps else len(gaps)}, интервал p50 {q(gaps,.5):.1f} мс мин {min(gaps) if gaps else float('nan'):.1f} | запуск после пробуждения sysmon p50/p99 {q(delay['sysmon'],.5):.2f}/{q(delay['sysmon'],.99):.2f} мс, прочие p99 {q(delay['P']+delay['idleM'],.99):.2f} | sysmonM={ns}")
