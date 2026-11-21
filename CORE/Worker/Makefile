BINARY := mod-worker
VERSION := $(shell git describe --tags --always 2>/dev/null || echo "dev")
LDFLAGS := -s -w -X main.Version=$(VERSION)
DIST := dist

# FX.147.p2b — hash-pinned Go deps: -mod=readonly refuses to mutate go.mod
# or fetch modules not listed in go.sum, so builds use exactly what go.sum
# pins. `export` passes it into every recipe's environment (go tool reads
# GOFLAGS); build config, not app env.
export GOFLAGS := -mod=readonly

.PHONY: all clean test lint version cross-build linux-amd64 linux-arm64 darwin-amd64 darwin-arm64 windows-amd64

all: linux-amd64 linux-arm64 darwin-amd64 darwin-arm64 windows-amd64

linux-amd64:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-linux-amd64 ./cmd/worker

linux-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-linux-arm64 ./cmd/worker

darwin-amd64:
	CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-darwin-amd64 ./cmd/worker

darwin-arm64:
	CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-darwin-arm64 ./cmd/worker

windows-amd64:
	CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-windows-amd64.exe ./cmd/worker

cross-build: linux-amd64 linux-arm64 darwin-amd64 darwin-arm64 windows-amd64

test:
	go test -v -race ./...

lint:
	go vet ./...

# sz-mac-worker.6b: the release version is CORE/Worker/VERSION (the file that
# cuts a release), NOT $(VERSION) above — that is a git tag/SHA stamp and is not
# a semver release. publish-worker-release.sh reads the same file.
version:
	@echo $(shell head -n1 $(CURDIR)/VERSION | tr -d '[:space:]')

clean:
	rm -rf $(DIST)
