---
sidebar_position: 4
title: "Open MOD to other computers on your network"
description: By default MOD Core (Community Edition) opens to your network at the address scripts/install.sh detects. Use scripts/install.sh to keep it on this computer only, to pick another address, and to switch back.
---

# Open MOD to other computers on your network

A fresh install with no flag opens MOD to your network at this computer's
detected address (the installer prints it once). `--no-lan` keeps it on this
computer only (`127.0.0.1`). This page shows how to choose, what the other
computer opens, and how to switch back.

## 1. The question and the flags

When you run `./scripts/install.sh` in a terminal it asks one question:
**"Open this install to other computers on your network? [Y/n]"**. Pressing
Enter accepts the network default; answer `n` to keep MOD on this computer only.
With no terminal and no flag, a fresh install opens to the detected address
(with no detectable address it stays on this computer only and prints a warning).
A re-run with no flag keeps a network address saved by an earlier `--lan` or
`--host` run and does not ask again. After a `--no-lan` run, a re-run in a
terminal asks again.

```bash
./scripts/install.sh --lan                  # open to the network, address auto-detected
./scripts/install.sh --host mod.office.lan  # open to the network, use this name or IP
./scripts/install.sh --no-lan               # this computer only
```

| Flag | What it does |
| --- | --- |
| `--lan` | Open to the network; the installer detects this computer's address. |
| `--no-lan` | This computer only. |
| `--host NAME_OR_IP` | Open to the network and use this name or IP (implies `--lan`). |
| `--bind ADDR` | With `--lan`: the address the ports listen on (default `0.0.0.0`). Ignored without `--lan`. |
| `--keycloak-port N` | The sign-in (Keycloak) port; default `8080`. |
| `--yes` | Do not ask; the no-flag default applies (opens to the detected address). |

## 2. What the other computer opens

Replace `<server-address>` with the address or name you used. The installer
prints the Dashboard and Workflow Builder addresses when it finishes.

| Port | What |
| --- | --- |
| `http://<server-address>:3000` | Dashboard (start here) |
| `http://<server-address>:3001` | Workflow Builder |
| `http://<server-address>:3002` | Docs |
| `http://<server-address>:8000` | API |
| `http://<server-address>:8080` | Sign-in (Keycloak); the `--keycloak-port` value if you set one |
| `<server-address>:7070` | File storage (it answers any browser origin by default through `VGW_CORS_ALLOW_ORIGIN`, so any web page can attempt cross-origin requests to it) |

These are the only ports that follow the network setting. The databases, Valkey,
OpenBao and the mail catcher stay on `127.0.0.1` (`./scripts/ce-doctor.sh`
shows this on its `bind-addrs` line).

The two optional compose profiles are not in that table. With the `registry`
profile on, the image registry listens on `0.0.0.0:5000` — every interface,
regardless of `--lan` — on purpose, so other worker machines can pull built nodes;
set `REGISTRY_BIND=127.0.0.1` to keep it on this machine. With the `stack-ingress`
profile on, the edge listens on `8880`/`8443` at the same address as the browser
ports above (`CE_BIND_ADDR`; `STACK_INGRESS_BIND` overrides it).

## 3. Firewall

The firewall on the MOD computer must allow the TCP ports in the table above
from your network. Traffic is plain HTTP, so use this on a network you trust
(home or office), not the open internet.

Sign-in to apps and stacks uses secure cookies. Browsers keep those only on
`https://` addresses and on `localhost`, so over this plain-HTTP LAN address
app and stack sign-in can fail or loop. To use apps or stacks from another
computer, put TLS in front of them (the `stack-ingress` profile with
`STACK_TLS_ISSUER` set; the edge's TLS port is `8443`), or open the MOD
computer's pages on `localhost`.

## 4. Switch an existing install

Run the installer again. It is safe to repeat: it rewrites only its own marked
block of `.env`, keeps your passwords and data, and restarts only what changed.

- Local to network: `./scripts/install.sh --lan` (or `--host NAME_OR_IP`).
- Network to local: `./scripts/install.sh --no-lan`.
- New address (for example a new DHCP lease): run `./scripts/install.sh --lan`
  again; it prints `address changed OLD -> NEW`.

Run every `docker compose` command below from the checkout root (the directory with `docker-compose.yml` and `.env`).

Plain `docker compose up -d` never opens the network by itself; it keeps what
the installer last wrote. Check the result any time with `./scripts/ce-doctor.sh`
(the `lan-hosts` and `bind-addrs` lines).

See [Getting Started](getting-started) for the rest of the install.
