//go:build !linux

package monitor

import "fmt"

func statfsImpl(path string, buf *syscallStatfs) error {
	return fmt.Errorf("statfs not implemented on this platform")
}
