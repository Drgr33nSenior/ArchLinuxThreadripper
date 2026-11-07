package retry

import (
	"testing"
	"time"
)

func TestDelay(t *testing.T) {
	cases := []struct {
		n    int
		want time.Duration
	}{
		{0, 0}, {1, 100 * time.Millisecond}, {2, 200 * time.Millisecond},
		{3, 400 * time.Millisecond}, {10, time.Second},
	}
	for _, c := range cases {
		if got := Delay(c.n, 100*time.Millisecond, time.Second); got != c.want {
			t.Errorf("Delay(%d) = %v, want %v", c.n, got, c.want)
		}
	}
}

func TestDelayNeverOverflowsPastMax(t *testing.T) {
	if got := Delay(80, time.Second, time.Minute); got != time.Minute {
		t.Errorf("Delay(80) = %v, want %v", got, time.Minute)
	}
}
