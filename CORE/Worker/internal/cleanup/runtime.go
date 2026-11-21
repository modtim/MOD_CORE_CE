package cleanup

import (
	"context"
	"os"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/api/types/system"
	"github.com/docker/docker/api/types/volume"
)

// dockerAPI is the exact subset of the docker client the cleanup runtime
// needs. *github.com/docker/docker/client.Client satisfies it; tests fake it.
type dockerAPI interface {
	ContainerList(ctx context.Context, options container.ListOptions) ([]types.Container, error)
	ImageList(ctx context.Context, options image.ListOptions) ([]image.Summary, error)
	VolumeList(ctx context.Context, options volume.ListOptions) (volume.ListResponse, error)
	NetworkList(ctx context.Context, options network.ListOptions) ([]types.NetworkResource, error)
	Info(ctx context.Context) (system.Info, error)

	ContainerRemove(ctx context.Context, id string, options container.RemoveOptions) error
	ImageRemove(ctx context.Context, imageID string, options image.RemoveOptions) ([]image.DeleteResponse, error)
	VolumeRemove(ctx context.Context, name string, force bool) error
	NetworkRemove(ctx context.Context, id string) error
}

// DockerRuntime adapts the docker client to the cleanup Runtime interface.
type DockerRuntime struct {
	d dockerAPI
}

// NewDockerRuntime wraps a docker client (*client.Client) for cleanup use.
func NewDockerRuntime(d dockerAPI) *DockerRuntime { return &DockerRuntime{d: d} }

// ListContainers returns every container (running and stopped), with full
// labels and image references.
func (r *DockerRuntime) ListContainers(ctx context.Context) ([]Container, error) {
	list, err := r.d.ContainerList(ctx, container.ListOptions{All: true})
	if err != nil {
		return nil, err
	}
	out := make([]Container, 0, len(list))
	for _, c := range list {
		out = append(out, Container{
			ID:        c.ID,
			Name:      firstName(c.Names),
			State:     c.State,
			ImageID:   c.ImageID,
			CreatedAt: time.Unix(c.Created, 0),
			Labels:    c.Labels,
		})
	}
	return out, nil
}

// ListImages returns every local image with its tags and size.
func (r *DockerRuntime) ListImages(ctx context.Context) ([]Image, error) {
	list, err := r.d.ImageList(ctx, image.ListOptions{})
	if err != nil {
		return nil, err
	}
	out := make([]Image, 0, len(list))
	for _, img := range list {
		if img.ID == "" {
			continue
		}
		out = append(out, Image{
			ID:        img.ID,
			RepoTags:  img.RepoTags,
			Size:      img.Size,
			CreatedAt: time.Unix(img.Created, 0),
		})
	}
	return out, nil
}

// ListVolumes returns the DANGLING local volumes (no container references
// them — the daemon's own definition of "unused") with labels. In-use
// volumes never appear, so the plan's UsedBy check is belt-and-braces.
func (r *DockerRuntime) ListVolumes(ctx context.Context) ([]Volume, error) {
	args := filters.NewArgs()
	args.Add("dangling", "true")
	resp, err := r.d.VolumeList(ctx, volume.ListOptions{Filters: args})
	if err != nil {
		return nil, err
	}
	out := make([]Volume, 0, len(resp.Volumes))
	for _, v := range resp.Volumes {
		if v == nil {
			continue
		}
		createdAt, _ := time.Parse(time.RFC3339Nano, v.CreatedAt)
		out = append(out, Volume{
			Name:      v.Name,
			CreatedAt: createdAt,
			Labels:    v.Labels,
		})
	}
	return out, nil
}

// ListNetworks returns every local network; Attached counts the containers
// on it.
func (r *DockerRuntime) ListNetworks(ctx context.Context) ([]Network, error) {
	list, err := r.d.NetworkList(ctx, network.ListOptions{})
	if err != nil {
		return nil, err
	}
	out := make([]Network, 0, len(list))
	for _, n := range list {
		createdAt := n.Created
		out = append(out, Network{
			ID:        n.ID,
			Name:      n.Name,
			CreatedAt: createdAt,
			Labels:    n.Labels,
			Attached:  len(n.Containers),
		})
	}
	return out, nil
}

// DockerRootUsage snapshots the partition backing the docker data root.
// Inside a containerized worker, statfs on "/" passes through overlayfs to
// the host filesystem backing /var/lib/docker; when the daemon reports a
// stat-able DockerRootDir, prefer it. ok=false on platforms without statfs
// or when the data root cannot be resolved — the planner treats that as
// "cannot trigger", never "empty".
func (r *DockerRuntime) DockerRootUsage(ctx context.Context) (RootUsage, bool) {
	path := "/"
	if info, err := r.d.Info(ctx); err == nil && info.DockerRootDir != "" {
		if _, serr := os.Stat(info.DockerRootDir); serr == nil {
			path = info.DockerRootDir
		}
	}
	return rootUsageOf(path)
}

// RemoveContainer force-removes a container.
func (r *DockerRuntime) RemoveContainer(ctx context.Context, id string) error {
	return r.d.ContainerRemove(ctx, id, container.RemoveOptions{Force: true})
}

// RemoveImage removes an image.
func (r *DockerRuntime) RemoveImage(ctx context.Context, id string) error {
	_, err := r.d.ImageRemove(ctx, id, image.RemoveOptions{})
	return err
}

// RemoveVolume removes a volume. The daemon refuses volumes still in use,
// which is the free safety net: a reclaim plan computed moments ago cannot
// outrun a container that attached in the meantime.
func (r *DockerRuntime) RemoveVolume(ctx context.Context, name string) error {
	return r.d.VolumeRemove(ctx, name, false)
}

// RemoveNetwork removes a network (refused while containers are attached).
func (r *DockerRuntime) RemoveNetwork(ctx context.Context, id string) error {
	return r.d.NetworkRemove(ctx, id)
}

func firstName(names []string) string {
	if len(names) == 0 {
		return ""
	}
	return names[0]
}

// rootUsageOf returns the used/total bytes of the partition holding path.
func rootUsageOf(path string) (RootUsage, bool) {
	total, used, ok := fsStat(path)
	if !ok {
		return RootUsage{}, false
	}
	return RootUsage{Total: total, Used: used}, true
}
