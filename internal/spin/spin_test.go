package spin

import (
	"testing"
	"time"
)

// Грубая проверка: после калибровки 2 мс работы занимают 2 мс ±30%.
// Точную проверку на целевой машине делает cmd/calib.
func TestCalibratedDuration(t *testing.T) {
	Calibrate(50*time.Millisecond, 3)
	start := time.Now()
	For(2 * time.Millisecond)
	el := time.Since(start)
	if el < 1400*time.Microsecond || el > 2600*time.Microsecond {
		t.Errorf("For(2ms) заняло %v", el)
	}
}
