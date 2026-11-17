// Package hist — лог-линейная гистограмма задержек (идея HdrHistogram, без зависимостей).
//
// Значения в наносекундах. Внутри каждой степени двойки 128 линейных подкорзин,
// поэтому относительная погрешность перцентиля не больше 1/128 ≈ 0,8%.
package hist

import (
	"encoding/json"
	"math"
	"math/bits"
	"sort"
)

const (
	subBits    = 7
	subBuckets = 1 << subBits // 128
	maxExp     = 40           // до 2^40 нс ≈ 18 минут
)

type Hist struct {
	counts [maxExp * subBuckets]uint64
	total  uint64
	min    int64
	max    int64
	sum    float64
}

func New() *Hist { return &Hist{min: math.MaxInt64} }

func index(v int64) int {
	if v < subBuckets {
		return int(v)
	}
	e := bits.Len64(uint64(v)) - subBits // >=1
	sub := int(v>>uint(e-1)) - subBuckets
	i := e*subBuckets + sub
	if i >= len(Hist{}.counts) {
		return len(Hist{}.counts) - 1
	}
	return i
}

// lower возвращает нижнюю границу корзины i.
func lower(i int) int64 {
	e := i / subBuckets
	sub := i % subBuckets
	if e == 0 {
		return int64(sub)
	}
	return int64(sub+subBuckets) << uint(e-1)
}

func (h *Hist) Record(v int64) {
	if v < 0 {
		v = 0
	}
	h.counts[index(v)]++
	h.total++
	h.sum += float64(v)
	if v < h.min {
		h.min = v
	}
	if v > h.max {
		h.max = v
	}
}

func (h *Hist) Merge(o *Hist) {
	for i, c := range o.counts {
		h.counts[i] += c
	}
	h.total += o.total
	h.sum += o.sum
	if o.min < h.min {
		h.min = o.min
	}
	if o.max > h.max {
		h.max = o.max
	}
}

func (h *Hist) Count() uint64 { return h.total }
func (h *Hist) Max() int64    { return h.max }

func (h *Hist) Mean() float64 {
	if h.total == 0 {
		return 0
	}
	return h.sum / float64(h.total)
}

// Quantile возвращает значение q-квантиля (0..1): верхнюю границу корзины,
// в которой набирается доля q, но не больше наблюдённого максимума.
func (h *Hist) Quantile(q float64) int64 {
	if h.total == 0 {
		return 0
	}
	target := uint64(math.Ceil(q * float64(h.total)))
	if target == 0 {
		target = 1
	}
	var acc uint64
	for i, c := range h.counts {
		acc += c
		if acc >= target {
			v := lower(i+1) - 1
			if v > h.max {
				v = h.max
			}
			return v
		}
	}
	return h.max
}

// Summary — стандартный набор перцентилей для отчётов.
type Summary struct {
	Count  uint64  `json:"count"`
	MeanNs float64 `json:"mean_ns"`
	P50    int64   `json:"p50_ns"`
	P90    int64   `json:"p90_ns"`
	P99    int64   `json:"p99_ns"`
	P999   int64   `json:"p999_ns"`
	P9999  int64   `json:"p9999_ns"`
	Max    int64   `json:"max_ns"`
}

func (h *Hist) Summary() Summary {
	return Summary{
		Count: h.total, MeanNs: h.Mean(),
		P50: h.Quantile(0.5), P90: h.Quantile(0.9), P99: h.Quantile(0.99),
		P999: h.Quantile(0.999), P9999: h.Quantile(0.9999), Max: h.max,
	}
}

// MarshalJSON сохраняет только ненулевые корзины: [[нижняя_граница_нс, count], ...].
// Этого достаточно, чтобы потом пересчитать любые перцентили и CDF в Python.
func (h *Hist) MarshalJSON() ([]byte, error) {
	type bucket [2]int64
	var bs []bucket
	for i, c := range h.counts {
		if c > 0 {
			bs = append(bs, bucket{lower(i), int64(c)})
		}
	}
	sort.Slice(bs, func(a, b int) bool { return bs[a][0] < bs[b][0] })
	return json.Marshal(struct {
		Summary Summary  `json:"summary"`
		Buckets []bucket `json:"buckets"`
	}{h.Summary(), bs})
}
