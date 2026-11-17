package hist

import (
	"math/rand"
	"sort"
	"testing"
)

func TestQuantileAccuracy(t *testing.T) {
	r := rand.New(rand.NewSource(1))
	h := New()
	vals := make([]int64, 200000)
	for i := range vals {
		vals[i] = int64(r.ExpFloat64() * 50_000) // экспонента со средним 50 мкс
		h.Record(vals[i])
	}
	sort.Slice(vals, func(a, b int) bool { return vals[a] < vals[b] })
	for _, q := range []float64{0.5, 0.9, 0.99, 0.999} {
		exact := vals[int(q*float64(len(vals)))-1]
		got := h.Quantile(q)
		rel := float64(got-exact) / float64(exact)
		if rel < -0.01 || rel > 0.01 {
			t.Errorf("q=%v: got %d, exact %d, rel err %.4f", q, got, exact, rel)
		}
	}
}

func TestBucketBounds(t *testing.T) {
	for _, v := range []int64{0, 1, 127, 128, 129, 255, 256, 1000, 1 << 20, 123456789} {
		i := index(v)
		if lo, hi := lower(i), lower(i+1); v < lo || v >= hi {
			t.Errorf("v=%d в корзине %d [%d,%d)", v, i, lo, hi)
		}
	}
}

func TestMerge(t *testing.T) {
	a, b := New(), New()
	a.Record(10)
	b.Record(1000)
	a.Merge(b)
	if a.Count() != 2 || a.Max() != 1000 {
		t.Fatalf("merge: count=%d max=%d", a.Count(), a.Max())
	}
}
