// Package dist — распределения времени обслуживания и межприходных интервалов.
package dist

import (
	"fmt"
	"math/rand"
	"strconv"
	"strings"
	"time"
)

// Dist выдаёт случайную длительность и знает своё среднее.
type Dist interface {
	Sample(r *rand.Rand) time.Duration
	Mean() time.Duration
}

type Const struct{ D time.Duration }

func (c Const) Sample(*rand.Rand) time.Duration { return c.D }
func (c Const) Mean() time.Duration             { return c.D }

type Exp struct{ M time.Duration }

func (e Exp) Sample(r *rand.Rand) time.Duration {
	return time.Duration(r.ExpFloat64() * float64(e.M))
}
func (e Exp) Mean() time.Duration { return e.M }

// Bimodal: с вероятностью P — Long, иначе Short (оба детерминированные).
type Bimodal struct {
	Short, Long time.Duration
	P           float64
}

func (b Bimodal) Sample(r *rand.Rand) time.Duration {
	if r.Float64() < b.P {
		return b.Long
	}
	return b.Short
}
func (b Bimodal) Mean() time.Duration {
	return time.Duration((1-b.P)*float64(b.Short) + b.P*float64(b.Long))
}

// Parse разбирает спецификацию:
//
//	const:50us
//	exp:50us
//	bimodal:10us,1ms,0.05   (short,long,p)
func Parse(spec string) (Dist, error) {
	kind, arg, ok := strings.Cut(spec, ":")
	if !ok {
		return nil, fmt.Errorf("dist %q: ожидается kind:args", spec)
	}
	switch kind {
	case "const", "exp":
		d, err := time.ParseDuration(arg)
		if err != nil {
			return nil, err
		}
		if kind == "const" {
			return Const{d}, nil
		}
		return Exp{d}, nil
	case "bimodal":
		parts := strings.Split(arg, ",")
		if len(parts) != 3 {
			return nil, fmt.Errorf("bimodal: нужно short,long,p")
		}
		s, err1 := time.ParseDuration(parts[0])
		l, err2 := time.ParseDuration(parts[1])
		p, err3 := strconv.ParseFloat(parts[2], 64)
		if err1 != nil || err2 != nil || err3 != nil {
			return nil, fmt.Errorf("bimodal %q: неверные аргументы", arg)
		}
		return Bimodal{s, l, p}, nil
	}
	return nil, fmt.Errorf("неизвестное распределение %q", kind)
}

// PoissonSchedule возвращает моменты прихода (смещения от старта) для пуассоновского
// потока интенсивности rate (в секунду) на интервале dur.
func PoissonSchedule(r *rand.Rand, rate float64, dur time.Duration) []time.Duration {
	if rate <= 0 {
		return nil
	}
	out := make([]time.Duration, 0, int(rate*dur.Seconds()*1.1)+16)
	var t float64
	limit := float64(dur)
	for {
		t += r.ExpFloat64() / rate * float64(time.Second)
		if t >= limit {
			return out
		}
		out = append(out, time.Duration(t))
	}
}
