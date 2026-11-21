package executor

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"
)

// sz-build-queue.4: the Worker-side half of the sandboxed build-queue runner.
// The API side (services/build_executor.py) validates a recipe against the
// frozen, argv-only registry from sz-build-queue.3 (commit 4fca7f65c) and
// stages the Dockerfile; this file takes the already-rendered argv steps and
// runs them inside an ephemeral, credential-free container.
//
// Deliberately NOT wired into the Backend interface (backend.go) — that
// contract is for the node-task execution path (K8s-reduction-engine Seam
// 1). A build-queue task is a different shape (no GPU, no S3 staging, no
// egress policy — it just runs a fixed argv list against a build context
// directory and reports exit code + a log artifact), so BuildKitExecutor is
// a small, standalone type a caller (the eventual build-queue task handler,
// sz-build-queue.5+) instantiates directly.
//
// Credential posture: BuildKitExecutor never sets container Env and never
// mounts a docker/buildx config or any credential file — RegistryAuth /
// Harbor push is explicitly out of scope here (sz-build-queue.5 owns the
// finalize/token/push flow). NetworkMode is "none": the container can't
// reach a registry even if a step's argv tried to.

// DefaultBuildKitImage is the sandbox image BuildKitExecutor runs recipe
// steps in when the caller doesn't override it.
//
// This is the rootless standalone variant, NOT moby/buildkit:buildx-stable-1.
// That image's entrypoint is buildkitd/buildctl (a daemon + thin client) —
// it does not contain a `docker` CLI, so the image_build recipe's rendered
// argv ("docker", "buildx", "build", ...) cannot run in it directly.
// RunBuild translates that argv into the equivalent
// `buildctl-daemonless.sh build --frontend dockerfile.v0 ...` invocation
// (see translateBuildxArgv) before using it as the container Cmd, and this
// const points at the image that script ships in.
const DefaultBuildKitImage = "moby/buildkit:rootless"

// DefaultBuildTimeout caps a build-queue run when BuildTaskSpec.Timeout is
// zero. Matches the image_build recipe's own timeout_seconds in
// services/recipe_registry.py.
const DefaultBuildTimeout = 30 * time.Minute

// DefaultBuildMemoryBytes / DefaultBuildPidsLimit bound one sandboxed build
// container's resource usage so a runaway or malicious build step can't
// exhaust the host.
const DefaultBuildMemoryBytes = 4 * 1024 * 1024 * 1024 // 4 GiB
const DefaultBuildPidsLimit = int64(512)

// BuildTaskSpec describes one build-queue task to run, as handed off by the
// API's BuildExecutorService.prepare_task() (task_id + rendered steps +
// context dir); the API validates/renders, the Worker only executes.
type BuildTaskSpec struct {
	// TaskID identifies the build-queue task; used for container labeling
	// and the artifact file name. Caller is responsible for giving it a
	// filesystem-safe value (the API side already restricts task_id to
	// [A-Za-z0-9_-]).
	TaskID string

	// RecipeID is informational (surfaced in errors/logs); BuildKitExecutor
	// does not branch on it — it runs whatever argv Steps[0] contains.
	RecipeID string

	// Steps are the recipe's already-rendered argv lists (recipe_registry.
	// render() output). Exactly one step is supported today (every current
	// recipe in sz-build-queue.3 has len(steps) == 1); RunBuild rejects
	// anything else rather than silently running only the first step.
	Steps [][]string

	// ContextDir is the host path to the build context, bind-mounted
	// read-only at /workspace inside the BuildKit container.
	ContextDir string

	// ArtifactDir is the host directory the combined stdout/stderr log is
	// written into (created if missing). The artifact file is named
	// "<TaskID>.log".
	ArtifactDir string

	// Timeout bounds the run; DefaultBuildTimeout is used when zero.
	Timeout time.Duration
}

// BuildResult is what RunBuild reports back to its caller.
type BuildResult struct {
	ExitCode     int
	ArtifactPath string
	// Err carries executor-side failure detail (container create/start/wait
	// errors) distinct from a nonzero ExitCode, which is a normal build
	// failure, not an executor error.
	Err string
}

// BuildKitExecutor spawns a BuildKit-capable container per task, with no
// credentials baked in, and captures its exit code + combined log.
type BuildKitExecutor struct {
	docker        *client.Client
	buildkitImage string
}

// NewBuildKitExecutor constructs a BuildKitExecutor. buildkitImage == ""
// falls back to DefaultBuildKitImage.
func NewBuildKitExecutor(docker *client.Client, buildkitImage string) *BuildKitExecutor {
	if buildkitImage == "" {
		buildkitImage = DefaultBuildKitImage
	}
	return &BuildKitExecutor{docker: docker, buildkitImage: buildkitImage}
}

// translateBuildxArgv converts the image_build recipe's rendered
// "docker buildx build ..." argv (services/recipe_registry.py, frozen as of
// sz-build-queue.3) into the equivalent buildctl-daemonless.sh invocation
// that actually runs inside DefaultBuildKitImage.
//
// Only -f/--file (Dockerfile name) and -t/--tag (image ref) are carried
// over. --builder is dropped (standalone buildctl has no builder-instance
// concept); --provenance=false/--sbom=false are dropped (buildctl's
// `type=image` output already omits both by default); the trailing
// positional context-dir argument is dropped because the actual build
// context comes from BuildTaskSpec.ContextDir, which the caller bind-mounts
// at /workspace — not from this argv token.
func translateBuildxArgv(step []string) ([]string, error) {
	if len(step) < 3 || step[0] != "docker" || step[1] != "buildx" || step[2] != "build" {
		return nil, fmt.Errorf(
			`build_executor: unsupported argv shape, expected "docker buildx build ...", got %v`, step,
		)
	}

	var dockerfile, tag string
	args := step[3:]
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "-f", "--file":
			if i+1 >= len(args) {
				return nil, errors.New("build_executor: -f/--file flag missing a value")
			}
			dockerfile = args[i+1]
			i++
		case "-t", "--tag":
			if i+1 >= len(args) {
				return nil, errors.New("build_executor: -t/--tag flag missing a value")
			}
			tag = args[i+1]
			i++
		case "--builder":
			i++ // value dropped, see doc comment above
		default:
			// --provenance=false / --sbom=false / --pull=false / the
			// trailing context-dir positional: all dropped, see doc
			// comment above.
		}
	}
	if dockerfile == "" {
		return nil, errors.New("build_executor: -f/--file value not found in argv")
	}
	if tag == "" {
		return nil, errors.New("build_executor: -t/--tag value not found in argv")
	}

	return []string{
		"buildctl-daemonless.sh", "build",
		"--frontend", "dockerfile.v0",
		"--local", "context=/workspace",
		"--local", "dockerfile=/workspace",
		"--opt", "filename=" + dockerfile,
		"--output", "type=image,name=" + tag + ",push=false",
	}, nil
}

// resolveTimeout returns spec.Timeout, falling back to DefaultBuildTimeout
// when unset.
func resolveTimeout(spec *BuildTaskSpec) time.Duration {
	if spec.Timeout <= 0 {
		return DefaultBuildTimeout
	}
	return spec.Timeout
}

// newContainerConfig builds the (pure, side-effect-free) container.Config
// for one build-queue run. No Env is ever set: the sandbox gets zero
// credentials (sz-build-queue.5 owns push/auth, out of scope here).
func newContainerConfig(image string, cmd []string, spec *BuildTaskSpec) *container.Config {
	return &container.Config{
		Image:      image,
		Cmd:        cmd,
		WorkingDir: "/workspace",
		Labels: map[string]string{
			"mod.component": "build-executor",
			"mod.task_id":   spec.TaskID,
			"mod.recipe_id": spec.RecipeID,
		},
	}
}

// newHostConfig builds the (pure, side-effect-free) container.HostConfig
// for one build-queue run.
//
// Privileged is intentionally left unset (defaults false) and no docker
// socket is ever bind-mounted. CapDrop removes every Linux capability.
// SecurityOpt relaxes seccomp/AppArmor specifically because rootless
// BuildKit needs to create user namespaces and overlay mounts inside the
// container to do its own build isolation — both docs upstream
// (https://github.com/moby/buildkit/blob/master/docs/rootless.md)
// recommend exactly this combination as the non-privileged alternative to
// --privileged. /dev/fuse is the one device rootless BuildKit needs for its
// snapshotter; nothing else is mapped in. ReadonlyRootfs is NOT set: the
// embedded buildkitd needs a writable rootfs for its own state/tmp dirs,
// and there's no bind-mounted tmpfs substitute in scope here — the
// stronger boundaries (no privileged, no docker socket, CapDrop ALL,
// NetworkMode none, memory/pids limits, container removed after every run)
// are what this sandbox actually relies on.
func newHostConfig(spec *BuildTaskSpec) *container.HostConfig {
	pidsLimit := DefaultBuildPidsLimit
	return &container.HostConfig{
		Binds:       []string{spec.ContextDir + ":/workspace:ro"},
		NetworkMode: "none",
		AutoRemove:  false, // removed explicitly below so logs can be read after exit
		CapDrop:     []string{"ALL"},
		SecurityOpt: []string{"no-new-privileges", "seccomp=unconfined", "apparmor=unconfined"},
		Resources: container.Resources{
			Memory:    DefaultBuildMemoryBytes,
			PidsLimit: &pidsLimit,
			Devices: []container.DeviceMapping{
				{PathOnHost: "/dev/fuse", PathInContainer: "/dev/fuse", CgroupPermissions: "rwm"},
			},
		},
	}
}

// RunBuild creates, starts, waits on, and tears down one sandboxed BuildKit
// container for spec, then writes its combined stdout/stderr to
// "<ArtifactDir>/<TaskID>.log" and returns the exit code + artifact path.
//
// The container is created with no Env, no registry/credential mounts, and
// NetworkMode "none" — it cannot reach any registry, matching the "no
// Harbor push from this task" boundary (push lives in sz-build-queue.5).
func (b *BuildKitExecutor) RunBuild(ctx context.Context, spec *BuildTaskSpec) (*BuildResult, error) {
	if spec == nil {
		return nil, errors.New("build_executor: nil BuildTaskSpec")
	}
	if spec.TaskID == "" {
		return nil, errors.New("build_executor: TaskID is required")
	}
	if len(spec.Steps) != 1 {
		return nil, fmt.Errorf(
			"build_executor: task %s recipe %s: expected exactly 1 rendered step, got %d",
			spec.TaskID, spec.RecipeID, len(spec.Steps),
		)
	}
	step := spec.Steps[0]
	if len(step) == 0 {
		return nil, fmt.Errorf("build_executor: task %s: empty step argv", spec.TaskID)
	}
	if spec.ContextDir == "" {
		return nil, fmt.Errorf("build_executor: task %s: ContextDir is required", spec.TaskID)
	}
	if spec.ArtifactDir == "" {
		return nil, fmt.Errorf("build_executor: task %s: ArtifactDir is required", spec.TaskID)
	}

	cmd, err := translateBuildxArgv(step)
	if err != nil {
		return nil, fmt.Errorf("build_executor: task %s: %w", spec.TaskID, err)
	}

	runCtx, cancel := context.WithTimeout(ctx, resolveTimeout(spec))
	defer cancel()

	resp, err := b.docker.ContainerCreate(runCtx,
		newContainerConfig(b.buildkitImage, cmd, spec),
		newHostConfig(spec),
		nil, nil, "",
	)
	if err != nil {
		return &BuildResult{Err: fmt.Sprintf("container create failed: %v", err)}, nil
	}

	defer func() {
		rmCtx, rmCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer rmCancel()
		_ = b.docker.ContainerRemove(rmCtx, resp.ID, container.RemoveOptions{Force: true})
	}()

	if err := b.docker.ContainerStart(runCtx, resp.ID, container.StartOptions{}); err != nil {
		return &BuildResult{Err: fmt.Sprintf("container start failed: %v", err)}, nil
	}

	statusCh, errCh := b.docker.ContainerWait(runCtx, resp.ID, container.WaitConditionNotRunning)
	var exitCode int
	select {
	case werr := <-errCh:
		if werr != nil {
			return &BuildResult{Err: fmt.Sprintf("container wait failed: %v", werr)}, nil
		}
	case status := <-statusCh:
		exitCode = int(status.StatusCode)
	}

	artifactPath, logErr := b.writeArtifactLog(runCtx, resp.ID, spec.ArtifactDir, spec.TaskID)
	if logErr != nil {
		return &BuildResult{ExitCode: exitCode, Err: fmt.Sprintf("artifact write failed: %v", logErr)}, nil
	}

	return &BuildResult{ExitCode: exitCode, ArtifactPath: artifactPath}, nil
}

// writeArtifactLog reads the container's combined stdout/stderr and writes
// it to "<artifactDir>/<taskID>.log", returning the written path.
func (b *BuildKitExecutor) writeArtifactLog(ctx context.Context, containerID, artifactDir, taskID string) (string, error) {
	if err := os.MkdirAll(artifactDir, 0o755); err != nil {
		return "", fmt.Errorf("mkdir artifact dir: %w", err)
	}

	logReader, err := b.docker.ContainerLogs(ctx, containerID, container.LogsOptions{
		ShowStdout: true,
		ShowStderr: true,
	})
	if err != nil {
		return "", fmt.Errorf("container logs: %w", err)
	}
	defer logReader.Close()

	artifactPath := filepath.Join(artifactDir, taskID+".log")
	f, err := os.Create(artifactPath)
	if err != nil {
		return "", fmt.Errorf("create artifact file: %w", err)
	}
	defer f.Close()

	if _, err := stdcopy.StdCopy(f, f, logReader); err != nil {
		return "", fmt.Errorf("copy container logs: %w", err)
	}
	return artifactPath, nil
}
