package tray

import (
	"testing"
)

// fakeProvider is an in-memory Provider for exercising the controller's
// state→icon mapping and menu action dispatch without a systray, a GUI, or a
// live worker.
type fakeProvider struct {
	name    string
	health  HealthState
	paused  bool
	manager string
	quit    bool

	setPausedCalls []bool
}

func (f *fakeProvider) Name() string        { return f.name }
func (f *fakeProvider) Health() HealthState { return f.health }
func (f *fakeProvider) Paused() bool        { return f.paused }
func (f *fakeProvider) SetPaused(v bool) {
	f.setPausedCalls = append(f.setPausedCalls, v)
	f.paused = v
}
func (f *fakeProvider) ManagerURL() string { return f.manager }
func (f *fakeProvider) Quit()              { f.quit = true }

// newTestController builds a Controller whose openURL is a recorder.
func newTestController(f *fakeProvider, opened *[]string) *Controller {
	c := NewController(f, nil)
	c.openURL = func(url string) { *opened = append(*opened, url) }
	return c
}

func TestModelIconMapsEachState(t *testing.T) {
	// On any build, Model().Icon must be the OS-appropriate bytes for the
	// state. iconBytes is the platform hook, so compare against it directly —
	// this is the load-bearing assertion (each state selects a distinct icon).
	want := map[HealthState]func(HealthState) []byte{
		StateOK:       iconBytes,
		StateDegraded: iconBytes,
		StateError:    iconBytes,
		StatePaused:   iconBytes,
	}
	seen := map[string]bool{}
	for st := range want {
		f := &fakeProvider{name: "w", health: st}
		c := NewController(f, nil)
		got := c.Model().Icon
		if len(got) == 0 {
			t.Fatalf("state %s: empty icon bytes", st.Label())
		}
		if string(got) != string(iconBytes(st)) {
			t.Fatalf("state %s: Model().Icon != iconBytes(state)", st.Label())
		}
		if seen[string(got)] {
			t.Fatalf("state %s: icon bytes duplicate another state", st.Label())
		}
		seen[string(got)] = true
	}
}

func TestEmbeddedIconsAreNonEmptyAndDistinct(t *testing.T) {
	// The four embedded PNGs and four embedded ICOs must each be non-empty and
	// pairwise distinct, otherwise the icon states would be indistinguishable.
	sets := [][]string{
		{string(okPNG), string(degradedPNG), string(errorPNG), string(pausedPNG)},
		{string(okICO), string(degradedICO), string(errorICO), string(pausedICO)},
	}
	for i, set := range sets {
		seen := map[string]bool{}
		for _, s := range set {
			if s == "" {
				t.Fatalf("set %d: empty embedded icon", i)
			}
			if seen[s] {
				t.Fatalf("set %d: duplicate embedded icon", i)
			}
			seen[s] = true
		}
	}
}

func TestModelPauseLabel(t *testing.T) {
	// Unpaused: shows "Pause", unchecked.
	c := NewController(&fakeProvider{paused: false}, nil)
	m := c.Model()
	if m.PauseTitle != "Pause" {
		t.Errorf("unpaused PauseTitle = %q, want %q", m.PauseTitle, "Pause")
	}
	if m.PauseChecked {
		t.Errorf("unpaused PauseChecked = true, want false")
	}
	if m.Tooltip == "" {
		t.Errorf("unpaused Tooltip empty")
	}

	// Paused: shows "Resume", checked.
	c = NewController(&fakeProvider{paused: true}, nil)
	m = c.Model()
	if m.PauseTitle != "Resume" {
		t.Errorf("paused PauseTitle = %q, want %q", m.PauseTitle, "Resume")
	}
	if !m.PauseChecked {
		t.Errorf("paused PauseChecked = false, want true")
	}
}

func TestModelTooltipIncludesNameAndState(t *testing.T) {
	c := NewController(&fakeProvider{name: "box1", health: StateError}, nil)
	tooltip := c.Model().Tooltip
	if tooltip != "box1 — error" {
		t.Errorf("tooltip = %q, want %q", tooltip, "box1 — error")
	}
	// Empty name falls back to a generic label.
	c = NewController(&fakeProvider{name: "", health: StateOK}, nil)
	if got := c.Model().Tooltip; got != "MOD worker — ok" {
		t.Errorf("empty-name tooltip = %q, want %q", got, "MOD worker — ok")
	}
}

func TestDispatchOpenManager(t *testing.T) {
	var opened []string
	f := &fakeProvider{manager: "http://127.0.0.1:47821/?token=abc"}
	c := newTestController(f, &opened)
	c.Dispatch(ActOpenManager)
	if len(opened) != 1 || opened[0] != f.manager {
		t.Fatalf("openURL calls = %v, want [%s]", opened, f.manager)
	}

	// No manager URL -> nothing opened, no panic.
	var opened2 []string
	c2 := newTestController(&fakeProvider{manager: ""}, &opened2)
	c2.Dispatch(ActOpenManager)
	if len(opened2) != 0 {
		t.Fatalf("openURL calls = %v, want none", opened2)
	}
}

func TestDispatchTogglePause(t *testing.T) {
	// From unpaused, toggle -> SetPaused(true).
	f := &fakeProvider{paused: false}
	c := newTestController(f, &[]string{})
	c.Dispatch(ActTogglePause)
	if len(f.setPausedCalls) != 1 || f.setPausedCalls[0] != true {
		t.Fatalf("SetPaused calls = %v, want [true]", f.setPausedCalls)
	}
	// Now paused, toggle again -> SetPaused(false).
	c.Dispatch(ActTogglePause)
	if len(f.setPausedCalls) != 2 || f.setPausedCalls[1] != false {
		t.Fatalf("SetPaused calls = %v, want [true false]", f.setPausedCalls)
	}
	if f.paused {
		t.Fatalf("fake ended paused, want unpaused after two toggles")
	}
}

func TestDispatchQuit(t *testing.T) {
	f := &fakeProvider{}
	c := newTestController(f, &[]string{})
	c.Dispatch(ActQuit)
	if !f.quit {
		t.Fatal("Quit() not called on Quit action")
	}
}

func TestDispatchUnknownIsNoop(t *testing.T) {
	var opened []string
	f := &fakeProvider{manager: "http://x"}
	c := newTestController(f, &opened)
	c.Dispatch(Action("nope"))
	if len(opened) != 0 || f.quit || len(f.setPausedCalls) != 0 {
		t.Fatal("unknown action should have no effect")
	}
}
