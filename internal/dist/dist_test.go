package dist

import (
	"math"
	"math/rand"
	"testing"
	"time"
)

func TestParseAndMean(t *testing.T) {
	r := rand.New(rand.NewSource(1))
	for _, spec := range []string{"const:50us", "exp:50us", "bimodal:10us,1ms,0.05"} {
		d, err := Parse(spec)
		if err != nil {
			t.Fatal(err)
		}
		var sum float64
		const n = 200000
		for i := 0; i < n; i++ {
			sum += float64(d.Sample(r))
		}
		got, want := sum/n, float64(d.Mean())
		if math.Abs(got-want)/want > 0.02 {
			t.Errorf("%s: выборочное среднее %v, ожидалось %v", spec, time.Duration(got), d.Mean())
		}
	}
}

func TestPoissonRate(t *testing.T) {
	r := rand.New(rand.NewSource(2))
	s := PoissonSchedule(r, 10000, 10*time.Second)
	if n := float64(len(s)); math.Abs(n-100000)/100000 > 0.02 {
		t.Errorf("ожидалось ~100000 приходов, получено %v", n)
	}
	for i := 1; i < len(s); i++ {
		if s[i] < s[i-1] {
			t.Fatal("расписание не монотонно")
		}
	}
}
