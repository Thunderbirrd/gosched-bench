// Package spin даёт откалиброванную CPU-работу без системных вызовов и аллокаций.
//
// Цикл не содержит вызовов функций, поэтому снять его с P может только
// асинхронное вытеснение рантайма (sysmon, ~10 мс) — это и нужно для BE-работы.
package spin

import (
	"sort"
	"time"
)

// Sink не даёт компилятору выбросить цикл.
var Sink uint64

// itersPerUs задаётся Calibrate. Значение по умолчанию заведомо грубое.
var itersPerUs float64 = 100

// Loop выполняет n итераций xorshift.
func Loop(n int64) {
	x := Sink | 1
	for i := int64(0); i < n; i++ {
		x ^= x << 13
		x ^= x >> 7
		x ^= x << 17
	}
	Sink = x
}

// For крутит CPU примерно d (по откалиброванной скорости, а не по часам:
// вызовы time.Now внутри цикла добавили бы точки, которых нет в реальной работе).
func For(d time.Duration) {
	Loop(int64(float64(d.Nanoseconds()) / 1000 * itersPerUs))
}

// Calibrate измеряет скорость цикла: медиана из rounds замеров по probe.
// Запускать на ненагруженной машине, до старта нагрузки.
func Calibrate(probe time.Duration, rounds int) float64 {
	const n = 1 << 20
	Loop(n) // прогрев
	rates := make([]float64, 0, rounds)
	for r := 0; r < rounds; r++ {
		var iters int64
		start := time.Now()
		for time.Since(start) < probe {
			Loop(n)
			iters += n
		}
		el := time.Since(start)
		rates = append(rates, float64(iters)/(float64(el.Nanoseconds())/1000))
	}
	sort.Float64s(rates)
	itersPerUs = rates[len(rates)/2]
	return itersPerUs
}

// ItersPerUs возвращает текущую калибровку.
func ItersPerUs() float64 { return itersPerUs }

// SetItersPerUs позволяет задать калибровку явно (например, из cmd/calib).
func SetItersPerUs(v float64) { itersPerUs = v }
