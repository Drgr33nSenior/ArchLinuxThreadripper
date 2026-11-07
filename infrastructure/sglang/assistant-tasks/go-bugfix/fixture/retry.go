// Package retry computes bounded exponential backoff delays.
package retry

import "time"

// Delay returns the wait before retry attempt n (n >= 1): base doubled for
// each earlier retry, never above max. Attempts below 1 return zero.
func Delay(n int, base, max time.Duration) time.Duration {
	if n < 1 {
		return 0
	}
	delay := base
	for i := 0; i < n; i++ {
		delay *= 2
		if delay > max {
			return max
		}
	}
	return delay
}
