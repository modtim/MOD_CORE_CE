package tray

// Unit tests for the platform-neutral tray event loop (menu.go) and the
// shared menu/state model. They need no display, no DBus, and no systray
// library: the native tray is replaced by a fake MenuSink that records paints
// and serves scripted clicks. These run identically on every build (linux
// default and tagged desktop, windows, darwin) because the logic under test
// carries no build tag.

import (
	"context"
	"testing"
	"time"
)

// menuFakeProvider is an in-memory Provider (tray_test.go keeps its own
// Provider fake for the controller tests; the two files stay independent).
type menuFakeProvider struct {
	name    string
	health  HealthState
	paused  bool
	manager string
	quit    bool

	setPausedCalls []bool
}

func (f *menuFakeProvider) Name() string        { return f.name }
func (f *menuFakeProvider) Health() HealthState { return f.health }
func (f *menuFakeProvider) Paused() bool        { return f.paused }
func (f *menuFakeProvider) SetPaused(v bool) {
	f.setPausedCalls = append(f.setPausedCalls, v)
	f.paused = v
}
func (f *menuFakeProvider) ManagerURL() string { return f.manager }
func (f *menuFakeProvider) Quit()              { f.quit = true }

// fakeSink records paints and answers clicks from a scripted queue per
// action. An empty queue means "the wait ended for another reason" (the
// worker is shutting down), which is how the loop learns to stop following an
// item.
type fakeSink struct {
	painted  []string
	quitSeen bool
	queued   map[Action][]bool
}

func (s *fakeSink) Paint(m Model) { s.painted = append(s.painted, m.Tooltip) }

func (s *fakeSink) Wait(_ context.Context, a Action) bool {
	q := s.queued[a]
	if len(q) == 0 {
		return false
	}
	yes := q[0]
	s.queued[a] = q[1:]
	return yes
}

func (s *fakeSink) Quit() { s.quitSeen = true }

func newSink(q map[Action][]bool) *fakeSink { return &fakeSink{queued: q} }

// doneContext is an already-cancelled context: the tests use it so the loop's
// goroutines (when it spawns them) terminate on their own and every assertion
// runs on the test's own thread.
func doneContext() context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	return ctx
}

func TestFollowDispatchesScriptedClicks(t *testing.T) {
	f := &menuFakeProvider{name: "box1", manager: "http://127.0.0.1:47821/?token=abc"}
	var opened []string
	c := NewController(f, nil)
	c.openURL = func(u string) { opened = append(opened, u) }

	sink := newSink(map[Action][]bool{
		ActOpenManager: {true},
		ActTogglePause: {true, true},
		ActQuit:        {true},
		})
	l := NewMenuLoop(c, sink, nil)
	ctx := doneContext()
	l.Follow(ctx, ActOpenManager)
	l.Follow(ctx, ActTogglePause)
	l.Follow(ctx, ActQuit)

	if len(opened) != 1 || opened[0] != f.manager {
		t.Fatalf("open-manager clicks opened %v, want [%s]", opened, f.manager)
	}
	if len(f.setPausedCalls) != 2 || f.setPausedCalls[0] != true || f.setPausedCalls[1] != false {
		t.Fatalf("pause clicks -> SetPaused %v, want [true false]", f.setPausedCalls)
	}
	if !f.quit {
		t.Fatal("quit click did not reach Provider.Quit")
	}
	if !sink.quitSeen {
		t.Fatal("quit click did not tear the tray down (MenuSink.Quit)")
	}
}

func TestFollowStopsWhenWaitEnds(t *testing.T) {
	// No scripted clicks: every Wait returns false, so the followers exit and
	// the loop does not spin or dispatch.
	f := &menuFakeProvider{name: "box1"}
	sink := newSink(map[Action][]bool{})
	l := NewMenuLoop(NewController(f, nil), sink, nil)
	ctx := doneContext()
	l.Follow(ctx, ActQuit)
	if f.quit || len(f.setPausedCalls) != 0 || sink.quitSeen {
		t.Fatal("un-clicked tray must not dispatch anything")
	}
}

func TestSyncRepaintsUntilDone(t *testing.T) {
	// A live context with a short deadline: Sync must repaint on the tick and
	// return once the context is cancelled (no spin).
	f := &menuFakeProvider{name: "box1"}
	sink := newSink(map[Action][]bool{})
	l := NewMenuLoop(NewController(f, nil), sink, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	l.Sync(ctx, time.Millisecond)
	cancel()
	if len(sink.painted) == 0 {
		t.Fatal("Sync painted nothing before ctx was done")
	}
}

func TestRunPaintsBeforeFirstTick(t *testing.T) {
	// Run must paint once up front so the tray is correct before the first
	// sync tick, and that first paint must carry the current state's tooltip.
	f := &menuFakeProvider{name: "box1", paused: true}
	sink := newSink(map[Action][]bool{})
	NewMenuLoop(NewController(f, nil), sink, nil).Run(doneContext())
	if len(sink.painted) == 0 {
		t.Fatal("Run painted nothing")
	}
	if sink.painted[0] != "box1 — ok" {
		t.Fatalf("first paint tooltip = %q, want %q", sink.painted[0], "box1 — ok")
	}
}

func TestMenuLoopModelMatchesController(t *testing.T) {
	// The loop never invents its own menu: what it paints is exactly what the
	// shared Controller's Model says.
	f := &menuFakeProvider{name: "box1", paused: false, health: StateDegraded}
	c := NewController(f, nil)
	sink := newSink(map[Action][]bool{})
	NewMenuLoop(c, sink, nil).Run(doneContext())
	if sink.painted[0] != c.Model().Tooltip {
		t.Fatalf("painted tooltip %q != Controller.Model().Tooltip %q", sink.painted[0], c.Model().Tooltip)
	}
}
