package api

import "io"
import "os"

// verify redline C: unchecked error (GO-2)
func verifyUnchecked() {
	io.WriteString(os.Stdout, "redline")
}
