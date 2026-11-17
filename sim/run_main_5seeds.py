import sys,csv,json
from multiprocessing import Pool
sys.path.insert(0,'/home/claude/gb/sim'); import mgk
rows=[r for r in csv.DictReader(open('/mnt/user-data/uploads/gosched-bench/results-aws/main/points.csv')) if float(r['beta'])>0]
SEEDS=[11,12,13,14,15]
jobs=[]; seen=set()
for r in rows:
    k=(r['rho'],r['beta'],r['P'])
    for s in SEEDS:
        jobs.append((r['rho'],r['beta'],r['P'],r['q'],'fcfs',s))
        if float(r['q'].rstrip('ms'))>10: jobs.append((r['rho'],r['beta'],r['P'],r['q'],'rr',s))   # при q ≤ 10 мс rr = fcfs
        if (k,s) not in seen: seen.add((k,s)); jobs.append((r['rho'],r['beta'],r['P'],r['q'],'prio',s))  # prio не зависит от q
def f(j):
    rho,beta,P,q,d,s=j
    return dict(rho=rho,beta=beta,P=P,q=q,disc=d,seed=s,p99_ns=mgk.run_point(int(P),float(rho),float(beta),"exp:63.6us",q,d,"10ms",1_000_000,s)['p99_ns'])
if __name__=='__main__':
    print(len(jobs),flush=True)
    with Pool(2) as p, open('sim5.jsonl','w') as o:
        for x in p.imap_unordered(f,jobs,chunksize=4): o.write(json.dumps(x)+'\n'); o.flush()
