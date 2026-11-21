package main

import (
	"context"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"
)

// pinnedVRAMThresholdMB is the VRAM a task must hold to be classed as pinned
// (discrete-GPU) memory that pausing cannot return to the human.
const pinnedVRAMThresholdMB = 256.0

// containerLifecycle is the subset of *executor.Executor this controller
// needs. It exists as an interface (rather than depending on the concrete
// executor type directly) so unit tests can substitute a fake that never
// touches a real Docker daemon; *executor.Executor satisfies this unchanged,
// so production callers pass it exactly as before.
type containerLifecycle interface {
	PauseContainer(ctx context.Context, containerID string) error
	UnpauseContainer(ctx context.Context, containerID string) error
	EvictContainer(containerID, reason string)
}

// preemptController yields running tasks when a human returns. Swappable-memory
// tasks pause (and resume on a blip, or evict after the grace window); tasks
// holding pinned discrete VRAM, or any task on a swapless box, evict at once —
// pausing cannot free that memory.
type preemptController struct {
	mu          sync.Mutex
	exec        containerLifecycle
	log         *slog.Logger
	graceFn     func() time.Duration
	tasks       map[int]*preemptTask
	swapPresent bool
	// cancelled holds task IDs the server asked us to kill before their
	// container existed (cancel arrived during pull/download). register()
	// checks this so a container created after the cancel is killed at once.
	cancelled map[int]bool
	// handback (24.5.s3) is the drain bookkeeping: drainAt is when the current
	// yield drain began (the first task given back), handbackRequeued counts
	// the in-flight harvest tasks this drain gave back to the manager. Both
	// are reported up the heartbeat as `noah_handback`: {at, tasks_requeued}
	// so the manager can tell an interrupted harvest apart from a task that
	// genuinely failed. resumeAll clears them — the drain is over.
	drainAt time.Time
	handbackRequeued int
	// preempted marks task IDs evicted by NOAH (yieldAll / evictAfterGrace)
	// rather than by a server-side hard-cancel (evictTasks). executeTask's
	// single container-failure report site reads this via wasPreempted to
	// decide whether to prefix the failure with the "[noah-preempted] "
	// sentinel, so the API requeues without charging a retry attempt.
	preempted map[int]bool
}

type preemptTask struct {
	containerID string
	vramMB      func() float64
	paused      bool
	graceTimer  *time.Timer
	// mode is the GPU allocation mode: "shared" (task reserves reservedMB of a
	// shared card), "exclusive" (task owns the card, never budget-evicted) or
	// "" (unknown, registered via register() and never picked).
	mode string
	// reservedMB is the card budget reserved for this shared task; enforce
	// evicts a shared task that uses more than this.
	reservedMB int
}

func newPreemptController(exec containerLifecycle, graceFn func() time.Duration, log *slog.Logger) *preemptController {
	return &preemptController{
		exec:        exec,
		log:         log,
		graceFn:     graceFn,
		tasks:       map[int]*preemptTask{},
		cancelled:   map[int]bool{},
		preempted:   map[int]bool{},
		swapPresent: swapPresent(),
	}
}

func (c *preemptController) register(taskID int, containerID string, vramMB func() float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	// A cancel that arrived before the container existed: kill it now rather
	// than tracking it for preemption. The task is already dead server-side.
	if c.cancelled[taskID] {
		delete(c.cancelled, taskID)
		c.log.Info("hard-cancel: container created after cancel signal, evicting now", "task", taskID)
		go c.exec.EvictContainer(containerID, "hard-cancel: task cancelled server-side")
		return
	}
	c.tasks[taskID] = &preemptTask{containerID: containerID, vramMB: vramMB}
}

// registerGPU is register() for GPU budget enforcement: mode is "shared" or
// "exclusive" and reservedMB is the card budget the shared task holds.
func (c *preemptController) registerGPU(taskID int, containerID, mode string, reservedMB int, vramMB func() float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cancelled[taskID] {
		delete(c.cancelled, taskID)
		c.log.Info("hard-cancel: container created after cancel signal, evicting now", "task", taskID)
		go c.exec.EvictContainer(containerID, "hard-cancel: task cancelled server-side")
		return
	}
	c.tasks[taskID] = &preemptTask{containerID: containerID, vramMB: vramMB, mode: mode, reservedMB: reservedMB}
}

func (c *preemptController) unregister(taskID int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if t, ok := c.tasks[taskID]; ok {
		if t.graceTimer != nil {
			t.graceTimer.Stop()
		}
		delete(c.tasks, taskID)
	}
	delete(c.cancelled, taskID)
	delete(c.preempted, taskID)
}

// wasPreempted reports whether taskID was evicted by NOAH (as opposed to a
// server-side hard-cancel) and clears the entry — it is meant to be read
// exactly once, at the single container-failure report site in executeTask.
func (c *preemptController) wasPreempted(taskID int) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	v := c.preempted[taskID]
	delete(c.preempted, taskID)
	return v
}

// evictTasks hard-kills the containers of tasks the server has cancelled. For a
// task whose container has not been created yet (cancel raced the pull), it
// records the intent so register() kills the container the moment it appears.
func (c *preemptController) evictTasks(taskIDs []int, reason string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, id := range taskIDs {
		t, ok := c.tasks[id]
		if !ok {
			// Container not registered yet — remember so register() kills it.
			c.cancelled[id] = true
			continue
		}
		if t.graceTimer != nil {
			t.graceTimer.Stop()
			t.graceTimer = nil
		}
		c.log.Info("hard-cancel: evicting task container", "task", id, "reason", reason)
		go c.exec.EvictContainer(t.containerID, reason)
		delete(c.tasks, id)
	}
}

// pickOverBudget returns the SHARED task most over its reservation: the task
// with the largest positive used(c.containerID) − reservedMB. It never picks
// exclusive or unknown-mode ("") tasks, and never a task with reservedMB<=0.
// used reports current card usage in MiB per container.
func pickOverBudget(tasks map[int]*preemptTask, used func(containerID string) int64) (taskID int, overMB int64, ok bool) {
	bestOver := int64(0)
	for id, t := range tasks {
		if t == nil || t.mode != "shared" || t.reservedMB <= 0 {
			continue
		}
		over := used(t.containerID) - int64(t.reservedMB)
		if over > 0 && over > bestOver {
			bestOver, taskID, ok = over, id, true
		}
	}
	return taskID, bestOver, ok
}

// enforceCardBudget evicts the shared task most over its reservation when the
// card's total usage (all containers) exceeds cardTotalMB − slackMB. Returns
// the evicted task ID and true when a task was evicted.
func (c *preemptController) enforceCardBudget(cardUsedMB, cardTotalMB, slackMB int64, used func(string) int64) (int, bool) {
	if cardUsedMB <= cardTotalMB-slackMB {
		return 0, false
	}
	c.mu.Lock()
	id, _, ok := pickOverBudget(c.tasks, used)
	var reason string
	if ok {
		t := c.tasks[id]
		reason = fmt.Sprintf("over_vram_budget: used %d MiB > reserved %d MiB", used(t.containerID), t.reservedMB)
	}
	c.mu.Unlock() // evictTasks takes the lock itself
	if !ok {
		return 0, false
	}
	c.log.Info("GPU budget enforcement evicting task", "task", id, "reason", reason, "card_used_mib", cardUsedMB, "card_total_mib", cardTotalMB)
	c.evictTasks([]int{id}, reason)
	return id, true
}

// yieldAll is called when the machine flips to "in use". Pauses what can be
// paused, evicts what cannot.
func (c *preemptController) yieldAll() {
	c.mu.Lock()
	defer c.mu.Unlock()
	grace := c.graceFn()
	 now := time.Now()
	 for id, t := range c.tasks {
		if t.paused {
			continue
		}
		pinned := !c.swapPresent
		if t.vramMB != nil && t.vramMB() > pinnedVRAMThresholdMB {
			pinned = true
		}
		if pinned {
			c.countHandback(now)
		 c.log.Info("NOAH evict (pinned memory, cannot pause)", "task", id)
			c.preempted[id] = true
			go c.exec.EvictContainer(t.containerID, "NOAH: human returned, pinned memory")
			continue
		}
		if err := c.exec.PauseContainer(context.Background(), t.containerID); err != nil {
			c.countHandback(now)
		 c.log.Warn("NOAH pause failed, evicting", "task", id, "error", err)
			c.preempted[id] = true
			go c.exec.EvictContainer(t.containerID, "NOAH: pause failed")
			continue
		}
		t.paused = true
		tid, cid := id, t.containerID
		t.graceTimer = time.AfterFunc(grace, func() { c.evictAfterGrace(tid, cid) })
	}
}

// countHandback (24.5.s3) records that one in-flight harvest task was given
// back to its human during the current drain. Callers already hold c.mu.
// drainAt is stamped on the FIRST hand-back of a drain, so the heartbeat's
// `noah_handback.at` is the moment lending stopped, not the moment the last
// task finished its grace.
func (c *preemptController) countHandback(now time.Time) {
	 if c.drainAt.IsZero() {
	  c.drainAt = now
	 }
	 c.handbackRequeued++
}

// handbackReport is the drain snapshot for the heartbeat. ok is false while
// no drain has handed anything back.
func (c *preemptController) handbackReport() (at time.Time, tasksRequeued int, ok bool) {
	 c.mu.Lock()
	 defer c.mu.Unlock()
	 if c.drainAt.IsZero() {
	  return time.Time{}, 0, false
	 }
	 return c.drainAt, c.handbackRequeued, true
}

func (c *preemptController) evictAfterGrace(taskID int, containerID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	t, ok := c.tasks[taskID]
	if !ok || !t.paused {
		return // resumed within grace, or already gone
	}
	c.countHandback(time.Now())
	 c.log.Info("NOAH grace expired, evicting paused task", "task", taskID)
	c.preempted[taskID] = true
	go c.exec.EvictContainer(containerID, "NOAH: human still active past grace window")
}

// resumeAll is called when the machine goes quiet again. Unpauses held tasks
// and cancels their grace timers.
//
// 24.5.s3: resumeAll is where a drain ENDS (the gate's onResume fires here),
// so it is also where the hand-back report is cleared — the manager stops
// seeing `noah_handback` once the machine is lending again, and the next
// drain starts its own count from zero.
func (c *preemptController) resumeAll() {
	// 24.5.s3: the drain is over (the machine went quiet again) — clear the
	// hand-back report so the heartbeat stops sending `noah_handback` and the
	// next drain starts its count from zero.
	c.drainAt = time.Time{}
	c.handbackRequeued = 0
	c.mu.Lock()
	defer c.mu.Unlock()
	for id, t := range c.tasks {
		if !t.paused {
			continue
		}
		if t.graceTimer != nil {
			t.graceTimer.Stop()
			t.graceTimer = nil
		}
		if err := c.exec.UnpauseContainer(context.Background(), t.containerID); err != nil {
			c.log.Warn("NOAH resume failed", "task", id, "error", err)
		}
		t.paused = false
	}
}

// swapPresent is platform-specific (preempt_linux.go reads /proc/meminfo,
// preempt_darwin.go asks the macOS VM, preempt_other.go covers windows and
// anything else) because /proc/meminfo does not exist on a Mac. The platform
// files all keep this exact name and signature: func swapPresent() bool.
//
// swapTotalKB parses a "SwapTotal:  8388604 kB" style line (the Linux reader
// and its tests share it; pure, so it runs on any host).
func swapTotalKB(line string) int64 {
	f := strings.Fields(line)
	if len(f) >= 2 {
		v, _ := strconv.ParseInt(f[1], 10, 64)
		return v
	}
	return 0
}

// vmStatDefaultPageSizeBytes is the page size used when a vm_stat reading does
// not name one (Apple Silicon is 4096; overridable in tests).
var vmStatDefaultPageSizeBytes = 4096

// vmStatAvailBytes turns a `vm_stat` reading into a conservative headroom
// estimate for the darwin swap probe: (free + available) pages x the page size
// the reading names. The file cache and inactive-clean pages are deliberately
// left out so the controller never claims headroom it cannot reclaim. Pure
// (no syscalls, no /proc) so the macOS logic is testable on any host.
func vmStatAvailBytes(raw string) uint64 {
 pageSize := int64(vmStatDefaultPageSizeBytes)
 var free, available int64
 for _, line := range strings.Split(raw, "\n") {
  lower := strings.ToLower(line)
  if strings.Contains(lower, "page size") {
   v := firstInt(strings.Fields(line))
   if v > 0 {
    pageSize = v
   }
   continue
  }
  if strings.Contains(lower, "free pages") || strings.Contains(lower, "pages free") {
   free = firstInt(strings.Fields(line))
   continue
  }
  if strings.Contains(lower, "available pages") || strings.Contains(lower, "pages available") {
   available = firstInt(strings.Fields(line))
  }
 }
 if free + available <= 0 || pageSize <= 0 {
  return 0
 }
 return uint64(free + available) * uint64(pageSize)
}

// firstInt reads the first number in a token list ("63045." -> 63045), 0 when
// there is none. It accepts both vm_stat spellings: the real macOS row puts the
// count BEFORE the label ("63045 free pages."), the captured fixtures in
// internal/monitor put it after ("Free pages: 63045").
func firstInt(f []string) int64 {
 for _, tok := range f {
  v, err := strconv.ParseInt(strings.Trim(strings.TrimRight(tok, "."), ","), 10, 64)
  if err == nil {
   return v
  }
 }
 return 0
}

// trailingInt reads the LAST number in a token list, for the "page size of N
// bytes" trailer where the count is the final token.
func trailingInt(f []string) int64 {
 if len(f) == 0 {
  return 0
 }
 v, _ := strconv.ParseInt(strings.Trim(strings.TrimRight(f[len(f)-1], "."), ","), 10, 64)
 return v
}
