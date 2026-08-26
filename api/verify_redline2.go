package api

import "io"
import "os"

// GO-2 负向实证：未检查 error（errcheck 应命中）
func verifyUnchecked() {
	io.WriteString(os.Stdout, "redline")
}
