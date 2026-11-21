// Package hostexec: output scrub for the host-exec stream (opsx worker-host-exec
// hx.8). A host process writes its stdout/stderr straight into the task-log outbox,
// so every frame must be redacted with EXACTLY the rules the native backend already
// applies to task logs (executor.scrubLogLine, reached through its exported wrapper
// ScrubLogLine) plus the credential shapes the host can leak that the native path
// never saw: AWS keys, key=value secrets and PEM private-key blocks.
//
// There is no import cycle: internal/executor does not import internal/hostexec, so
// this package may import executor and share one definition of the base rules.
package hostexec

import (
	"bytes"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"regexp"
)

// pemKeyPlaceholder replaces an entire PEM private-key block (BEGIN..END) that
// streamed through one frame.
const pemKeyPlaceholder = "<REDACTED PRIVATE KEY>"

// awsKeyPlaceholder replaces an AWS access-key ID (AKIA...).
const awsKeyPlaceholder = "<REDACTED AWS KEY>"

// redactedValuePlaceholder replaces the value half of a key=value secret.
const redactedValuePlaceholder = "<REDACTED>"

// awsAccessKeyPattern matches an AWS AccessKeyId (AKIA + 16 base32 chars), and
// the optional 40-char secret that follows it when both leak side by side.
var awsAccessKeyPattern = regexp.MustCompile(`AKIA[0-9A-Z]{16}([0-9A-Za-z/+=]{40})?`)

// awsKVPattern matches aws_access_key= / aws_secret_access_key= style settings
// (aws-cli, boto, .env files). It runs BEFORE awsAccessKeyPattern so the whole
// "key=value" pair collapses to one placeholder instead of leaving the value's
// tail dangling.
var awsKVPattern = regexp.MustCompile(`(?i)\baws_[a-z_]*(access|secret)[a-z_]*\s*[=:]\s*\S+`)

// secretKVPattern matches password= / token= / secret= (also "key": "value" JSON
// and KEY: value config shapes) and redacts the value only, keeping the key name
// so the log stays diagnosable.
var secretKVPattern = regexp.MustCompile(`(?i)(["']?\b(password|passwd|token|secret|api_key|apikey|access_token|refresh_token)\b["']?\s*[=:]\s*)["']?[^\s"',}\]]+`)

// pemBeginPattern / pemEndPattern bracket a PEM private-key block. The bracket
// match is line-level because a PEM body spans many stream lines.
var pemBeginPattern = regexp.MustCompile(`(?i)-{3,}[^-]*BEGIN[^-]*PRIVATE KEY[^-]*-{3,}`)

var pemEndPattern = regexp.MustCompile(`(?i)-{3,}[^-]*END[^-]*PRIVATE KEY[^-]*-{3,}`)

// ScrubFrame redacts one host-exec output frame. It is line-wise: complete lines
// are scrubbed and re-emitted with their newlines, and a trailing PARTIAL line
// (the frame cut mid-line) is kept intact — same bytes, no synthesised newline —
// so the next frame continues it seamlessly. Partial lines are still scrubbed
// fail-closed: a credential that happens to end at a frame boundary never leaks
// just because the newline landed in the next frame.
func ScrubFrame(data []byte, workspace string) []byte {
	if len(data) == 0 {
		return data
	}
	lines := bytes.Split(data, []byte("\n"))
	kept := [][]byte{}
	inPEM := false
	for _, ln := range lines {
		s := string(ln)
		if pemBeginPattern.MatchString(s) {
			// A PEM block starts on this line: emit one placeholder for the whole
			// block and swallow every line until the END marker (or EOF, which is
			// the fail-closed answer to a frame truncated mid-key).
			kept = append(kept, []byte(pemKeyPlaceholder))
			inPEM = !pemEndPattern.MatchString(s)
			continue
		}
		if inPEM {
			if pemEndPattern.MatchString(s) {
				inPEM = false
			}
			continue
		}
		kept = append(kept, []byte(scrubLine(s, workspace)))
	}
	// bytes.Split keeps a trailing empty part, so re-joining with '\n' is an
	// exact inverse: a frame that ended mid-line still ends mid-line (the next
	// frame continues it) and one that ended with a newline still ends with a
	// newline. Only the lines swallowed by a PEM block collapse away.
	return bytes.Join(kept, []byte("\n"))
}

// scrubLine applies the shared native rules (executor.ScrubLogLine: scratch-root
// paths + Bearer tokens) and then the host-only credential shapes. A line that
// matches nothing is returned unchanged.
func scrubLine(s, workspace string) string {
	out := executor.ScrubLogLine(s, workspace)
	out = awsKVPattern.ReplaceAllString(out, "aws_secret_key="+redactedValuePlaceholder)
	out = awsAccessKeyPattern.ReplaceAllString(out, awsKeyPlaceholder)
	out = secretKVPattern.ReplaceAllString(out, "$1"+redactedValuePlaceholder)
	return out
}
