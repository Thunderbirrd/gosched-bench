// Command calib калибрует spin-цикл и проверяет его точность на этой машине.
//
// Без калибровки все значения q и S в экспериментах условны. Запускать
// перед каждой серией (частота CPU может меняться между днями).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"runtime"
	"sort"
	"time"

	"gosched-bench/internal/spin"
)

var (
	probe  = flag.Duration("probe", 200*time.Millisecond, "длительность одного замера калибровки")
	rounds = flag.Int("rounds", 7, "число замеров (берётся медиана)")
	reps   = flag.Int("reps", 200, "повторов на каждую проверяемую длительность")
)

type check struct {
	Target string  `json:"target"`
	P50Err float64 `json:"p50_rel_err"`
	P99Err float64 `json:"p99_rel_err"`
	P50Ns  int64   `json:"p50_ns"`
	P99Ns  int64   `json:"p99_ns"`
}

func main() {
	flag.Parse()
	runtime.GOMAXPROCS(1) // мерить скорость одного ядра без помех

	var runs []float64
	for i := 0; i < 3; i++ {
		runs = append(runs, spin.Calibrate(*probe, *rounds))
		time.Sleep(200 * time.Millisecond)
	}
	sort.Float64s(runs)
	spin.SetItersPerUs(runs[1])
	spread := (runs[2] - runs[0]) / runs[1]

	var checks []check
	for _, target := range []time.Duration{50 * time.Microsecond, time.Millisecond, 10 * time.Millisecond} {
		n := *reps
		if target >= 10*time.Millisecond {
			n = max(20, n/10)
		}
		got := make([]int64, n)
		// Прогрев перед каждой группой: после sleep ядро (особенно в ВМ на Mac)
		// какое-то время работает медленнее, и короткие проверки целиком
		// попадали в этот разгон (пилот: +73…+110% на 50 мкс).
		spin.For(100 * time.Millisecond)
		for i := range got {
			t := time.Now()
			spin.For(target)
			got[i] = time.Since(t).Nanoseconds()
		}
		sort.Slice(got, func(a, b int) bool { return got[a] < got[b] })
		p50, p99 := got[n/2], got[n*99/100]
		checks = append(checks, check{
			Target: target.String(),
			P50Ns:  p50, P99Ns: p99,
			P50Err: float64(p50-target.Nanoseconds()) / float64(target.Nanoseconds()),
			P99Err: float64(p99-target.Nanoseconds()) / float64(target.Nanoseconds()),
		})
	}

	out := map[string]any{
		"go_version":   runtime.Version(),
		"iters_per_us": runs[1],
		"calib_runs":   runs,
		"calib_spread": spread, // разброс между тремя калибровками, доля
		"checks":       checks,
	}
	b, _ := json.MarshalIndent(out, "", "  ")
	fmt.Println(string(b))
	if spread > 0.03 {
		log.Printf("ВНИМАНИЕ: разброс калибровки %.1f%% > 3%%: проверьте губернатор частоты и turbo", spread*100)
		os.Exit(2)
	}
}
