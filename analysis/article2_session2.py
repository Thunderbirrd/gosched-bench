import sys,json,numpy as np; sys.path.insert(0,'/home/claude/gb/analysis'); import article2 as A
metal=A.load('ci'); vm=A.load('vm/envcmp'); c6=A.load('ci6')
out={}
def desc(runs):
    p=np.array([A.hd(r,0.99) for r in runs]); m=np.array([A.hd(r,0.5) for r in runs]); lo,hi=A.ci_two_level(runs,0.99)
    return dict(runs=len(runs),p50_us=A.us(m.mean()),p99_us=A.us(p.mean()),ci=[A.us(lo),A.us(hi)],sd_between_us=A.us(p.std(ddof=1)),cv=round(float(p.std(ddof=1)/p.mean()),3),min_us=A.us(p.min()),max_us=A.us(p.max()))
for q in('5ms','10ms'):
    out[f'metal q={q}']=desc(metal[('0.5',q,'open')]); out[f'vm q={q}']=desc(vm[('0.5',q,'open')])
    lo,hi=A.diff_ci(metal[('0.5',q,'open')],vm[('0.5',q,'open')],0.99); out[f'vm-metal q={q} P99 diff CI us']=[A.us(lo),A.us(hi)]
a=metal[('0.5','5ms','open')]; b=c6[('0.5','6ms','open')]
out['metal q=6ms']=desc(b)
sh=[]
for p in A.QS:
    d=np.mean([A.hd(x,p) for x in b])-np.mean([A.hd(x,p) for x in a]); lo,hi=A.diff_ci(a,b,p,1000); sh.append(dict(p=p,diff_us=A.us(d),lo_us=A.us(lo),hi_us=A.us(hi)))
out['shift 6ms-5ms']=sh
pw={}
for m in (3,5,10,20,30):
    hit=0
    for _ in range(A.TRIALS):
        sa=[a[j] for j in A.RNG.choice(len(a),m,replace=False)]; sb=[b[j] for j in A.RNG.choice(len(b),m,replace=False)]
        lo,hi=A.diff_ci(sa,sb,0.99,300); hit+= lo>0 or hi<0
    pw[m]=round(hit/A.TRIALS,3)
out['power P99 5ms vs 6ms']=pw
json.dump(out,open('article2_session2.json','w'),ensure_ascii=False,indent=1)
for k,v in out.items(): print(k,json.dumps(v,ensure_ascii=False))
