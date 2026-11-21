package executor

// stackrunner pre-flight — an unlabelled container squatting on a compose-chosen
// name must REFUSE the deploy (never auto-removed), naming the container in both
// the error and the log. The daemon is faked through the dockerCLI seam, exactly
// as stacknetheal_test.go does it.

import (
	"context"
	"fmt"
	"strings"
	"testing"
)

const orphanSpec = `
services:
  llama-server:
    image: llama
`

// runPreflight drives the pre-flight against a fake daemon. psOut is what the
// anchored `ps` returns ("" = no such container), labels what `inspect` reports,
// and listFails models a daemon hiccup on the list call. Returns the deploy
// error plus whatever the deploy log would show the operator.
func runPreflight(project, psOut, labels string, listFails bool) (error, []string) {
	orig := dockerCLI
	defer func() { dockerCLI = orig }()
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if args[0] == "ps" {
			if listFails {
				return "", fmt.Errorf("docker ps exited 1")
			}
			return psOut, nil
		}
		return labels, nil
	}
	var streamed []string
	err := (&Executor{}).preflightOrphanNames(context.Background(),
		Callbacks{OnLogBatch: func(l []string) { streamed = append(streamed, l...) }},
		project, orphanSpec)
	return err, streamed
}

func TestPreflightOrphanNames_RefusesUnlabelledCollision(t *testing.T) {
	proj := "modstack140g0"
	name := proj + "-llama-server-1"
	other := "com.docker.compose.tag=other;com.example.ha-class=stateless;"
	err, streamed := runPreflight(proj, name+"|llama|abc123", other, false)
	if err == nil {
		t.Fatalf("unlabelled name squat must refuse the deploy")
	}
	msg := err.Error()
	if !strings.Contains(msg, "stack deploy refused") || !strings.Contains(msg, "unlabelled container") {
		t.Fatalf("error %q must name the refusal and the container", msg)
	}
	if !strings.Contains(msg, name) || !strings.Contains(msg, "llama") {
		t.Fatalf("error %q must name container+image: %s", msg, name)
	}
	if len(streamed) != 1 || !strings.Contains(streamed[0], name) {
		t.Fatalf("log must name the container, got %v", streamed)
	}
	if !strings.Contains(streamed[0], "docker rm") {
		t.Fatalf("modstack-owned squat must carry the operator hint")
	}
}

func TestPreflightOrphanNames_OwnLabelProceeds(t *testing.T) {
	proj := "modstack140g0"
	name := proj + "-llama-server-1"
	own := "com.docker.compose.project=" + proj + ";com.docker.compose.service=llama-server;"
	err, streamed := runPreflight(proj, name+"|llama|abc123", own, false)
	if err != nil {
		t.Fatalf("compose-owned container must proceed: %v", err)
	}
	if len(streamed) != 0 {
		t.Fatalf("no log noise for a healthy deploy: %v", streamed)
	}
}

func TestPreflightOrphanNames_NoContainerProceeds(t *testing.T) {
	err, streamed := runPreflight("modstack7g0", "", "", false)
	if err != nil {
		t.Fatalf("nothing colliding must not fail: %v", err)
	}
	if len(streamed) != 0 {
		t.Fatalf("nothing colliding must not log: %v", streamed)
	}
}

func TestPreflightOrphanNames_ListErrorProceeds(t *testing.T) {
	err, streamed := runPreflight("modstack7g0", "", "", true)
	if err != nil {
		t.Fatalf("a daemon hiccup must not fail the deploy: %v", err)
	}
	if len(streamed) != 1 || !strings.Contains(streamed[0], "cannot list containers") {
		t.Fatalf("expected the list-failure note, got %v", streamed)
	}
}

func TestPreflightOrphanNames_NonModstackPrefixRefusesWithoutHint(t *testing.T) {
	proj := "tenant-abc"
	name := proj + "-llama-server-1"
	other := "com.docker.compose.tag=other;"
	err, streamed := runPreflight(proj, name+"|llama|abc123", other, false)
	if err == nil {
		t.Fatalf("a squat under any prefix still blocks compose up: must refuse")
	}
	if !strings.Contains(err.Error(), "unlabelled container") {
		t.Fatalf("error %q must name the squat", err.Error())
	}
	if len(streamed) != 1 || !strings.Contains(streamed[0], name) {
		t.Fatalf("log must name the container, got %v", streamed)
	}
	if strings.Contains(streamed[0], "docker rm") || strings.Contains(streamed[0], "docker stop") {
		t.Fatalf("must NOT offer a removal command outside the modstack prefix: %v", streamed)
	}
}
