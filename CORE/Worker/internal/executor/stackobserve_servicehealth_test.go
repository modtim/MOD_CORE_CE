package executor

// STACK-HEALTH-STATUS-1126: the worker's per-service posture for the manager's
// status_health derivation. The states these pin: a crash-looping service is
// bad; a one-shot that exited 0 with restart intent `no` is HEALTHY (owner
// decision: one-shot services exited 0, restart no are healthy); the daemon's
// RestartCount is evidence, never the rule; a running service whose healthcheck
// says unhealthy is bad without being exited.

import (
	"testing"
)

func TestServiceIsBad_ExitNonZeroIsBad(t *testing.T) {
	if !serviceIsBad("exited", "", "always", 1, 0) {
		t.Fatal("an exited service with a failing exit code must read as bad")
	}
	if !serviceIsBad("exited", "", "no", 137, 0) {
		t.Fatal("exit 137 is bad whatever the intent: OOM-killed is not a finished job")
	}
}

func TestServiceIsBad_OneShotExitZeroIsHealthy(t *testing.T) {
	// The owner rule: exited 0 + restart no = a finished one-shot, healthy.
	if serviceIsBad("exited", "", "no", 0, 0) {
		t.Fatal("a one-shot that exited 0 with intent `no` is a finished job, " +
			"not a broken service")
	}
	if serviceIsBad("exited", "", "false", 0, 0) {
		t.Fatal("`false` is Docker's spelling of the same intent")
	}
}

func TestServiceIsBad_ExitZeroWithRestartIntentIsBad(t *testing.T) {
	// exit 0 but Docker would restart it (always/unless-stopped/on-failure):
	// the service keeps re-launching and keeps finishing - a loop, not a job.
	if !serviceIsBad("exited", "", "always", 0, 4) {
		t.Fatal("intent `always` with exit 0 is a restart loop, bad")
	}
	if serviceIsBad("exited", "", "on-failure:3", 0, 2) {
	 t.Fatal("on-failure after a 0 exit does NOT restart (containerWantsRunning's " +
		"rule): a finished job, healthy")
	}
}

func TestServiceIsBad_HealthcheckUnhealthyWhileRunning(t *testing.T) {
	if !serviceIsBad("running", "unhealthy", "no", -1, 0) {
		t.Fatal("a running container whose healthcheck says unhealthy is bad")
	}
	if serviceIsBad("running", "healthy", "no", -1, 0) {
		t.Fatal("running + healthy is serving")
	}
	if serviceIsBad("running", "", "no", -1, 0) {
	 t.Fatal("running with no healthcheck is serving (no health data is not bad news)")
	}
}

func TestServiceIsBad_RestartingPausedDeadAreBad(t *testing.T) {
	if !serviceIsBad("restarting", "", "no", 0, 9) {
	 t.Fatal("restarting is crash-looping: must not read as up")
	}
	if !serviceIsBad("dead", "", "no", 0, 0) {
	 t.Fatal("dead is worse than exited")
	}
	if serviceIsBad("created", "", "no", -1, 0) {
	 t.Fatal("a never-started container is not a broken service - stackDownCount " +
		"already reports containers that should run")
	}
}

func TestServiceIsBad_RestartCountSeparatesLoopFromOneShot(t *testing.T) {
	// exited: restarts alone never condemn a finished one-shot (the count is
	// the daemon's history, the intent is the rule).
	if serviceIsBad("exited", "", "no", 0, 7) {
		t.Fatal("restarts=7 with intent no and exit 0 is still a finished one-shot")
	}
	// running: restarts>0 IS condemnation - the daemon restarted a crashed
	// container (a manual docker start never bumps RestartCount).
	if !serviceIsBad("running", "", "no", 0, 3) {
		t.Fatal("a running container the daemon already restarted twice is a " +
			"crash-loop mid-cycle: must read bad")
	}
}
