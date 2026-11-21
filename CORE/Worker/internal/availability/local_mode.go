package availability

// LocalMode is the wire vocabulary of the local lend/pause switch. It lives in
// its own file because the local UI (internal/localui) parses it independently
// of the availability evaluator that consumes it.

type LocalMode string

const (
	// LocalModeNone means "no local override": the automatic verdict rules. It is
	// the empty string so a fresh gate (never set) is naturally none.
	// LocalModeLend is "lend this machine": force available now, even while the
	// sampled signal/history says the machine is busy.
	// LocalModePause is "pause lending (I'm using it)": force unavailable.
	// Both spellings are the wire vocabulary the local UI setter accepts (see
	// ParseLocalMode) and the same words the tray/app UI shows.
	LocalModeNone  = ""
	LocalModeLend  = "lend"
	LocalModePause = "pause"
)

// LocalModeError is the setter's "not a mode at all" signal — the local UI turns
// it into a 400. Mirrors availability.go's timeError idiom.
type LocalModeError string

func (e LocalModeError) Error() string { return string(e) }

const errBadMode = LocalModeError("unknown local mode")

// ParseLocalMode maps a wire string to a LocalMode. Only lend, pause and
// "none" (clear the pin, back to the automatic rules) are accepted; anything
// else — including the empty string, i.e. a body that simply forgot the mode —
// is an error rather than a silent "none", so a typo can never quietly mean
// "stop overriding".
func ParseLocalMode(v string) (LocalMode, error) {
	switch v {
	case "none":
		// "none" is what the plain HTML form on the /noah page posts for the
		// "Automatic" button; it clears the pin and hands the decision back to
		// the automatic rules.
		return LocalModeNone, nil
	case LocalModeLend:
		return LocalModeLend, nil
	case LocalModePause:
		return LocalModePause, nil
	}
	return LocalModeNone, errBadMode
}
