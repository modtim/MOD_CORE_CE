# encrypted-store — in-container gocryptfs images for versitygw + Valkey (ENC-IMAGES)

Two build contexts, one shared entrypoint:

| Context | Image | Store |
|---|---|---|
| `versitygw-enc/Dockerfile` | `mcw-encstore/versitygw-enc` | versitygw v1.7.0 (binary copied out of the digest-pinned upstream image) |
| `valkey-enc/Dockerfile` | `mcw-encstore/valkey-enc` | valkey 8.1.10 (digest-pinned `valkey/valkey:8-alpine`) |
| `entrypoint-enc.sh` | shared by both | mount + guard + privilege drop + clean shutdown |

## Why this replaces the sidecar

The sidecar design (J1) mounts gocryptfs in a SEPARATE container and hands the view
to the store with `:rshared`/`:rslave` bind propagation. That works on native Linux
(`results-1110/SIDECAR-J5.md`, 6/6) but is **impossible on Docker Desktop / WSL2**:
`results-1110/SIDECAR-WSL2-PROBE.md` shows the daemon rejecting the bind before the
container starts — `path ... is mounted on / but it is not a shared mount` — while a
gocryptfs mount **inside** a container works there fine. A mount that lives in the
same container as the store needs no propagation at all, so it works on both.

versitygw has no built-in encryption (owner-confirmed): the store is a plain `posix`
backend over `/data`. Encryption at rest here is gocryptfs, nothing else.

## Supply-chain pins (verified on the Dell, rootless Docker, 2026-11-10)

| Pin | versitygw-enc | valkey-enc |
|---|---|---|
| store base | `ghcr.io/versity/versitygw:v1.7.0@sha256:c4cbd9d9…7ad` (binary source) | `valkey/valkey:8-alpine@sha256:081c2f5c…8507` |
| package base | `alpine:3.19@sha256:6baf4358…ca1` | (the valkey base itself, `/etc/alpine-release` = 3.24.2) |
| `gocryptfs` NEVRA | `gocryptfs-2.4.0-r7` (v3.19 community) | `gocryptfs-2.6.1-r7` (v3.24 community) |
| apk sha256 | `d914e4952fa6…0a5c` | `43a943b1f7f6…5402` |
| `fuse3` NEVRA | `fuse3-3.16.2-r0` (v3.19 main) | `fuse3-3.18.3-r0` (v3.24 main) |
| apk sha256 | `72009a8b616e…f718` | `75c11b645c91…4d15` |
| `util-linux` NEVRA | `util-linux-2.39.3-r0` (v3.19 main) | `util-linux-2.42.3-r1` (v3.24 main) |
| apk sha256 | `3b4981db8876…d28e` | `eb9246e52168…8908` |
| `gocryptfs --version` | `gocryptfs v2.4.0; go-fuse [vendored]; 2024-05-12 go1.21.10 linux/amd64` | `gocryptfs v2.6.1; go-fuse [vendored]; 2026-10-14 go1.26.9 linux/amd64` |

Full values live on the `ARG …=` lines of each Dockerfile (single source of truth);
the table is a convenience copy. Each `.apk` is downloaded once, its sha256 checked,
then installed with `apk add --force <local path>` — the CDN is never consulted a
second time, so a tampered mirror cannot change what installs.

**Why the two contexts pin different package releases:** the pinned valkey base is
Alpine 3.24.2, and a v3.19 `fuse3` apk cannot install on it
(`ERROR: unable to select packages: so:libfuse3.so.3 (no such package)` — the base
already carries a newer fuse3 soname). Each context pins the packages of ITS OWN
base release. `gocryptfs` is therefore 2.4.0 in `versitygw-enc` (exactly J1's pin)
and 2.6.1 in `valkey-enc` (the only gocryptfs in v3.24); the entrypoint uses only
options both accept (`-init -q -passfile= -allow_other -fg`).

## Privilege model

- The **gocryptfs daemon stays root** (FUSE mounts need `SYS_ADMIN`); the compose
  grants `cap_add: SYS_ADMIN`, the `/dev/fuse` device and `apparmor:unconfined` —
  the same three lines the sidecar services already carry.
- The **store runs unprivileged**: `setpriv --reuid=… --regid=… --clear-groups
  --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs`.
  The drop is **util-linux** `setpriv`, not the busybox applet, and the two images
  resolve it at **different paths**: on the Dell (2026-11-10) `/bin/setpriv` is a busybox
  symlink **only in the 3.19 base** (`versitygw-enc`: `/bin/setpriv -> /bin/busybox`), whose
  usage block carries only `-d/--dump`, `--nnp/--no-new-privs`, `--inh-caps`, `--ambient-caps`
  — it rejects `--reuid` and `--bounding-set` outright, and `--inh-caps=-all` there ("unknown
  capability 'all'"). In the pinned valkey base `/bin/setpriv` is **already** a real util-linux
  2.42.3 binary (34864 bytes) and there is **no** `/usr/bin/setpriv` at all. The pinned
  `util-linux` apk installs the util-linux `setpriv` at **`/usr/bin/setpriv` in `versitygw-enc`**
  (2.39.3, first on PATH — `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`) and
  leaves it at **`/bin/setpriv` in `valkey-enc`** (2.42.3 — that release ships it in `/bin` only);
  `command -v setpriv` resolves to the util-linux binary in each image, and both accept the
  whole flag set (proved: `setpriv … id` → `uid=1000 gid=1000`, rc 0).
  Both Dockerfiles already assert `[ -x /bin/setpriv ]`; the entrypoint's writability
  guard runs `setpriv --reuid=…` **before** the store starts, so a base that only had
  the busybox applet fails closed there instead of running privileged.
  Measured by ENC-PROOF4 on images `6e635a68b452` (`results-1110/ENC-PROOF4-gpu5.md`):
  the store process shows `CapBnd`, `CapInh`, `CapPrm`, `CapEff` and `CapAmb` all
  `0000000000000000` and `NoNewPrivs: 1` (it keeps **no** capability and cannot
  regain one; `--no-new-privs` also refuses setuid/setgid and file-capability
  execs).
- Store uid: versitygw's upstream image runs as **root**, so CE fixes the store user
  here at numeric **uid/gid 1000** (no `/etc/passwd` entry needed — `setpriv` takes
  numbers); valkey's own user is **uid 999 / gid 1000** (`id valkey` on the Dell).
  **The host cipher dir is `0700`** (CE default; proven on the Desktop: a 0700
  root-owned dir is refused by the guard below, 0777 works). The entrypoint chowns
  it to the store uid at first start (next sentence), so after the first start it
  is owned by the store uid, not root.
  The entrypoint, while it is still **root and before mounting**, chowns the
  cipher dir **root entry only** (non-recursive — the tree can hold terabytes,
  and the FUSE daemon runs as root anyway) to the store uid/gid and `chmod 0700`
  it, with one log line; gocryptfs then maps that owner+mode onto the `/data`
  view, so the store uid can write its own view without a world-writable host
  dir. A filesystem that ignores chown (a Windows/WSL bind) cannot be fixed this
  way: the entrypoint logs the failed chown and continues, and the guard below fails closed
  **only** if the store uid cannot write the view. A drvfs cipher dir at the CE-exact 0700 is
  refused; a drvfs dir at its usual 0777 **passes** — the store then runs with a **world-writable**
  cipher dir, i.e. the CE 0700 isolation is silently degraded. Keep CE cipher dirs on a filesystem
  that honours ownership (ext4 / WSL2-native, never `/mnt/c` drvfs).
  For **versitygw only** the small `/iam` state dir (a named volume, which Docker
  creates root-owned — proven: `open /iam/users.json: permission denied` for uid
  1000) is `chown -R -h`'d to the store uid before the drop (symlinks are never followed:
  the store uid owns and writes `/iam`, so a planted link must not redirect a root
  chown), and only when the top-level `/iam` owner differs from the store uid;
  nothing else is chowned, and the cipher dir is never chowned recursively.
  The root-only flag dir `/run/encstore` is created before the store starts (a
  failure there is fatal: the store is not running yet). A TERM that arrives
  between the signal trap and the moment the store pid is known is re-delivered
  to the store right after the pid is assigned, so it is never lost.

## Lifecycle (`entrypoint-enc.sh`, both images)

1. Fail closed if the passfile `/run/secrets/gocryptfs-pass` is missing or empty.
2. `gocryptfs -init -q` **exactly once**, only over a virgin cipher dir
   (`/ciphertext-raw`); a non-empty cipher dir without `gocryptfs.conf` is existing
   ciphertext → refuse, exit 1 (re-init would destroy the store).
3. **Mount-point guard**: `/data` must be **empty** before mounting — leftovers from an
   unclean unmount (or a store that wrote into the container layer) are refused fail-closed;
   recover with `docker compose rm <service>`, never by deleting the cipher dir.
4. **Ownership prep** (root, before the mount): chown the cipher dir root entry to
   the store uid/gid + `chmod 0700`; for versitygw also `chown -R /iam`.
5. Mount at the store's own dir **inside the container**:
   `gocryptfs -q -passfile=… -allow_other -fg /ciphertext-raw /data &` — **`-fg` +
   background**, so the FUSE daemon stays a child of PID 1 and PID 1 can supervise
   it (a daemonized mount reparents it and PID 1 never learns it died). Then a
   **bounded wait** (60 × 0.5 s) until `mountpoint -q /data` succeeds — otherwise
   refuse to start the store (that would write plaintext into the cipher dir). The
   `TERM INT` trap is installed **before** this wait, so a `docker stop` during
   startup exits in seconds instead of waiting for the SIGKILL that follows the TERM at the
   service's `stop_grace_period` (**30 s** in the CE compose). If a `TERM`/`INT` arrives during
   the mount wait, PID 1 exits **0** without ever starting the store (unmounting first if the
   mount did appear) — a `docker stop` during startup never leaves a half-started store.
6. **Enter the view**: `cd /data` after the mount guard, then a runtime self-check
   (`mountpoint -q /data`, `stat -c %d /data` vs `stat -c %d /`, `pwd -P` under
   `/data`) — all fail closed. **Why this matters (proven on xeon, ENC-PROOF-LINUX):**
   without the cd, the store's cwd stayed the underlying **unmounted** image dir
   `/data`, so Valkey `SET` + `SAVE` returned OK while `dump.rdb` was created in the
   container's **writable layer** — `docker diff <service>` showed `A /data/dump.rdb`
   and the plaintext marker was found in the overlay snapshot — and **nothing** landed
   in the cipher dir. Valkey also gets `--dir /data` appended (unless the compose
   already passed one); versitygw needs neither (its command uses absolute `/data`).
   **The check on any store image: `docker diff <service>` must show NO `/data`
   entries** — a `/data` entry means plaintext outside the mount. The same leak is
   why the xeon proof saw a permanent restart loop after the daemon died: the leaked
   plaintext sat in the layer under `/data` and tripped the "mount point /data is not
   empty" guard on every start. With the leak fixed that path should not occur; if it
   does, recover with `docker compose rm <service>` (discards the writable layer) and
   **never** delete or edit the cipher dir.
7. Run the store via `setpriv` with the capability drop above, backgrounded.
8. **PID 1 supervises both processes** (trap loop, chosen over `tini`: tini would
   be a 4th pinned package for no extra guarantee):
   - `trap … TERM INT` forwards `SIGTERM`/`SIGINT` to the store.
   - PID 1 then **waits for the store to really exit**: POSIX `wait` returns `>128`
     the moment a signal is trapped, **without** the child being gone, so exiting
     there would let PID 1 go and SIGKILL a store that is still flushing (Valkey
     final RDB save, versitygw writes). The loop re-`wait`s until `kill -0` says the
     store is gone and keeps the store's **real** exit code.
   - A watcher subshell (`kill -0` + `sleep 1`; busybox ash has no `wait -n`)
     watches the FUSE pid: if the daemon dies, `/data` is a dead mount, so it
     writes the fuse-dead flag (root-only `/run/encstore`, set BEFORE the TERM), TERMs the store, and **forces the store out**:
     it waits at most **`ENCSTORE_FUSE_DEAD_GRACE` seconds** (default **8**,
     integer, validated at startup; `kill -0` polled once per second) for the
     TERM'd store to exit, and if it is still alive sends **KILL**. **Why this
     is needed (proven on cpu4, `ENC-PROOF2-cpu4.md` claim G):** Valkey on a
     DEAD mount cannot write its final RDB — it logs `# Error trying to save
     the DB, can't exit.` and **refuses to exit at all**, so the unbounded wait
     hangs PID 1 forever: the container stays `running`, `ExitCode 0`,
     `RestartCount 0`, health `unhealthy`, `mountpoint -q /data` rc=32, and
     `restart: unless-stopped` never fires. The fuse-dead branch then reaps the
     store with `wait`, **lazy-unmounts** (`fusermount3 -uz`, best effort — an
     eager unmount cannot work over a dead mount) and exits **1**, so compose
     restarts the service. **Data-loss note:** nothing can be saved over a dead
     mount, so the store's final save was already impossible — whatever it
     changed since its **last successful save is LOST** (the ciphertext up to
     that point stays intact and readable). The grace is bounded **only** on
     this path: a plain `docker stop` on a **healthy** mount keeps the
     **unbounded** wait above, so the store's final flush is allowed to finish
     (docker's own stop timeout is the limit there). A `TERM` arriving during
     the fuse-dead handling only re-runs the trap (a second `TERM` on a store
     that is already being forced out) — that path is not a loop, so it cannot
     re-enter or hang.
   - Only **then** unmount: PID 1 first **`cd /`** out of the mount — the store's cwd
     is *inside* `/data` (the `cd "$DATA_DIR"` above is what makes Valkey write into the
     view), and a mount **cannot be unmounted non-lazily while any process has its cwd
     in it**, so without leaving it `fusermount3 -u` fails `Resource busy` every time and
     the retry loop burns ~5 s of the compose stop grace (the proof ran with docker's 5 s
     default; the **CE compose sets `stop_grace_period: 30s`** for both stores) before PID 1 is SIGKILLed
     (`ExitCode 137`, proven on the Dell, `ENC-PROOF2-local` claim F) — then
     `fusermount3 -u /data` retried a few times with a short sleep (FUSE can still be
     busy for a moment, now only for some **other** process holding the mount), then
     lazy `fusermount3 -uz` as last resort, and the log says which one worked; as a **last
     last resort** a plain `umount` is tried, and if nothing worked the log says
     `NOT unmounted - a stale FUSE mount may survive under the bind` while the entrypoint
     still exits with the store's own code.

## Healthcheck contract

Store's own check **plus** the mount check, in the same service:

    healthcheck:
      test: ["CMD-SHELL", "mountpoint -q /data && valkey-cli -a \"$REDIS_PASSWORD\" ping"]

versitygw has no CLI ping; the CE compose ANDs the mount check with reachability of **both**
its ports: `mountpoint -q /data && nc -z 127.0.0.1 7070 && nc -z 127.0.0.1 7071` (data port
7070 and admin port 7071).

## Compose change the lead must make

Both sidecar services go away; the stores take the encrypted mount themselves.
Image names: `${CE_IMAGE_REGISTRY:-ghcr.io/modtim}/versitygw-enc:${CE_IMAGE_TAG:-latest}`
and `${CE_IMAGE_REGISTRY:-ghcr.io/modtim}/valkey-enc:${CE_IMAGE_TAG:-latest}`.
Binds: cipher dir → `/ciphertext-raw`, passfile `:ro`, **no `:rshared`, no
`:rslave`, no decrypted host dir**. Exact lines are in
`results-1110/ENC-IMAGES.md`. Host paths are env-overridable: the cipher bind is
`${VERSITYGW_CIPHER_PATH:-/var/lib/mod/versitygw-ciphertext}/<tenant-slug>:/ciphertext-raw`
(per-tenant) and `${VALKEY_CIPHER_PATH:-/var/lib/mod/valkey-ciphertext}/data:/ciphertext-raw`;
the passfile is the host file `/run/secrets/gocryptfs-pass`, bind-mounted `:ro`. That path is on tmpfs (wiped by a reboot), so it is only a copy: the persistent key is `/var/lib/mod/gocryptfs-pass` (0600, root, beside the cipher dirs) and `first-run-sidecar.sh` / `recover-after-reboot.sh` rebuild the `/run` copy from it. A new key is never made over existing ciphertext.

## Build (Dell only — image builds never run on the fleet/test boxes)

    rsync -a CORE/DockerContainers/encrypted-store/ dell:/tmp/encstore-ctx/
    ssh dell 'cd /tmp/encstore-ctx && DOCKER_BUILDKIT=1 docker build -t mcw-encstore/versitygw-enc:1 -f versitygw-enc/Dockerfile .'
    ssh dell 'cd /tmp/encstore-ctx && DOCKER_BUILDKIT=1 docker build -t mcw-encstore/valkey-enc:1 -f valkey-enc/Dockerfile .'

`COPY ../entrypoint-enc.sh` cannot cross the context root, so each Dockerfile is
built **from the context root** with `-f <ctx>/Dockerfile` and copies
`entrypoint-enc.sh` from there.

Verified on the Dell 2026-11-10: `versitygw --version` → `v1.7.0`,
`valkey-server --version` → `8.1.10`, sizes 114 MB / 89.7 MB, and both images run
unprivileged with **no** passfile → `encstore: passfile … refusing to mount`, exit 1.
No mounts and no privileged runs on the Dell; the mount proofs are separate jobs
(native Linux on xeon, Docker Desktop on this machine).
