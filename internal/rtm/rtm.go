// Package rtm снимает метрики рантайма (runtime/metrics) и cgroup v2.
//
// Две функции:
//   - Sampler пишет строку CSV каждые interval (для графиков во времени);
//   - Snapshot/Delta дают итог за окно измерения (между SIGUSR1 и SIGTERM).
package rtm

import (
	"bufio"
	"fmt"
	"io"
	"math"
	"os"
	"runtime/metrics"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	MSchedLat   = "/sched/latencies:seconds"
	MGCPauses   = "/sched/pauses/total/gc:seconds"
	MAssist     = "/cpu/classes/gc/mark/assist:cpu-seconds"
	MGCDedic    = "/cpu/classes/gc/mark/dedicated:cpu-seconds"
	MGCIdle     = "/cpu/classes/gc/mark/idle:cpu-seconds"
	MUserCPU    = "/cpu/classes/user:cpu-seconds"
	MTotalCPU   = "/cpu/classes/total:cpu-seconds"
	MGCCycles   = "/gc/cycles/total:gc-cycles"
	MGoroutines = "/sched/goroutines:goroutines"
	MGomaxprocs = "/sched/gomaxprocs:threads"

	// /cpu/classes/* в runtime/metrics — оценки, обновляемые только на циклах GC.
	// Для учёта CPU процесса берём getrusage.
	RUserS = "rusage_user_s"
	RSysS  = "rusage_sys_s"
)

var names = []string{MSchedLat, MGCPauses, MAssist, MGCDedic, MGCIdle, MUserCPU, MTotalCPU, MGCCycles, MGoroutines, MGomaxprocs}

// Snapshot — состояние накопительных метрик в момент времени.
type Snapshot struct {
	At        time.Time
	SchedLat  *metrics.Float64Histogram
	GCPauses  *metrics.Float64Histogram
	Scalars   map[string]float64 // cpu-seconds, циклы GC и т.п.
	Throttled CgroupStat
}

func Take() Snapshot {
	s := make([]metrics.Sample, len(names))
	for i, n := range names {
		s[i].Name = n
	}
	metrics.Read(s)
	snap := Snapshot{At: time.Now(), Scalars: map[string]float64{}, Throttled: ReadCgroup()}
	for _, x := range s {
		switch x.Value.Kind() {
		case metrics.KindFloat64Histogram:
			h := x.Value.Float64Histogram()
			cp := &metrics.Float64Histogram{Counts: append([]uint64(nil), h.Counts...), Buckets: h.Buckets}
			if x.Name == MSchedLat {
				snap.SchedLat = cp
			} else {
				snap.GCPauses = cp
			}
		case metrics.KindFloat64:
			snap.Scalars[x.Name] = x.Value.Float64()
		case metrics.KindUint64:
			snap.Scalars[x.Name] = float64(x.Value.Uint64())
		}
	}
	var ru syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_SELF, &ru) == nil {
		snap.Scalars[RUserS] = float64(ru.Utime.Nano()) / 1e9
		snap.Scalars[RSysS] = float64(ru.Stime.Nano()) / 1e9
	}
	return snap
}

// HistDelta — разность двух снимков гистограммы, с перцентилями.
type HistDelta struct {
	Count   uint64       `json:"count"`
	P50     float64      `json:"p50_s"`
	P99     float64      `json:"p99_s"`
	P999    float64      `json:"p999_s"`
	Max     float64      `json:"max_s"`   // верхняя граница последней непустой корзины
	Buckets [][2]float64 `json:"buckets"` // [[нижняя_граница_с, count], ...] только ненулевые
}

// fin заменяет ±Inf на конечные значения (JSON их не поддерживает).
func fin(v, fallback float64) float64 {
	if math.IsInf(v, 0) {
		return fallback
	}
	return v
}

func histDelta(a, b *metrics.Float64Histogram) HistDelta {
	var d HistDelta
	if a == nil || b == nil {
		return d
	}
	counts := make([]uint64, len(b.Counts))
	for i := range b.Counts {
		counts[i] = b.Counts[i] - a.Counts[i]
		d.Count += counts[i]
		if counts[i] > 0 {
			lo := fin(b.Buckets[i], 0)
			d.Buckets = append(d.Buckets, [2]float64{lo, float64(counts[i])})
			d.Max = fin(b.Buckets[i+1], lo)
		}
	}
	q := func(p float64) float64 {
		if d.Count == 0 {
			return 0
		}
		target := uint64(p*float64(d.Count) + 0.999999)
		var acc uint64
		for i, c := range counts {
			acc += c
			if acc >= target {
				return fin(b.Buckets[i+1], fin(b.Buckets[i], 0)) // верхняя граница корзины
			}
		}
		return d.Max
	}
	d.P50, d.P99, d.P999 = q(0.5), q(0.99), q(0.999)
	return d
}

// Delta — итог за окно между двумя снимками.
type Delta struct {
	WindowS     float64            `json:"window_s"`
	SchedLat    HistDelta          `json:"sched_latencies"`
	GCPauses    HistDelta          `json:"gc_pauses"`
	Scalars     map[string]float64 `json:"scalars"` // приращения
	NrThrottled int64              `json:"nr_throttled"`
	ThrottledUs int64              `json:"throttled_usec"`
	CgroupFound bool               `json:"cgroup_found"`
}

func Diff(a, b Snapshot) Delta {
	d := Delta{
		WindowS:  b.At.Sub(a.At).Seconds(),
		SchedLat: histDelta(a.SchedLat, b.SchedLat),
		GCPauses: histDelta(a.GCPauses, b.GCPauses),
		Scalars:  map[string]float64{},
	}
	for k, v := range b.Scalars {
		if k == MGoroutines || k == MGomaxprocs {
			d.Scalars[k] = v // мгновенные значения, не приращения
			continue
		}
		d.Scalars[k] = v - a.Scalars[k]
	}
	d.CgroupFound = b.Throttled.Found
	d.NrThrottled = b.Throttled.NrThrottled - a.Throttled.NrThrottled
	d.ThrottledUs = b.Throttled.ThrottledUsec - a.Throttled.ThrottledUsec
	return d
}

// CgroupStat — счётчики троттлинга из cpu.stat (cgroup v2).
type CgroupStat struct {
	Found         bool
	NrThrottled   int64
	ThrottledUsec int64
}

// ReadCgroup читает cpu.stat текущей cgroup. На macOS и без cgroup v2 Found=false.
func ReadCgroup() CgroupStat {
	var st CgroupStat
	path := "/sys/fs/cgroup/cpu.stat"
	if b, err := os.ReadFile("/proc/self/cgroup"); err == nil {
		// cgroup v2: строка вида "0::/system.slice/run-xxx.scope"
		for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
			if rel, ok := strings.CutPrefix(line, "0::"); ok {
				path = "/sys/fs/cgroup" + rel + "/cpu.stat"
			}
		}
	}
	f, err := os.Open(path)
	if err != nil {
		return st
	}
	defer f.Close()
	st.Found = true
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		k, v, _ := strings.Cut(sc.Text(), " ")
		n, _ := strconv.ParseInt(v, 10, 64)
		switch k {
		case "nr_throttled":
			st.NrThrottled = n
		case "throttled_usec":
			st.ThrottledUsec = n
		}
	}
	return st
}

// Sampler пишет в w строку CSV каждые interval, пока не закрыт stop.
// Колонки: время, приращения за интервал и перцентили задержки планирования.
func Sampler(w io.Writer, interval time.Duration, stop <-chan struct{}) {
	fmt.Fprintln(w, "unix_s,window_s,sched_p50_s,sched_p99_s,sched_p999_s,gc_pauses,gc_cycles,assist_cpu_s,gc_dedicated_cpu_s,rusage_user_s,rusage_sys_s,goroutines,nr_throttled,throttled_usec")
	prev := Take()
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
		}
		cur := Take()
		d := Diff(prev, cur)
		fmt.Fprintf(w, "%.3f,%.3f,%.9f,%.9f,%.9f,%d,%.0f,%.6f,%.6f,%.6f,%.6f,%.0f,%d,%d\n",
			float64(cur.At.UnixNano())/1e9, d.WindowS,
			d.SchedLat.P50, d.SchedLat.P99, d.SchedLat.P999,
			d.GCPauses.Count, d.Scalars[MGCCycles], d.Scalars[MAssist], d.Scalars[MGCDedic],
			d.Scalars[RUserS], d.Scalars[RSysS], d.Scalars[MGoroutines],
			d.NrThrottled, d.ThrottledUs)
		prev = cur
	}
}
