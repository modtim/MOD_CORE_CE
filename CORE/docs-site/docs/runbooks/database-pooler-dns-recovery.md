---
sidebar_position: 57
slug: /runbooks/database-pooler-dns-recovery
title: Check the database after a restart
description: "Runbook: Community Edition ships no transaction-pooling proxy — the API connects directly to the `postgres-app` service — so the cached-DNS strand this page describes cannot occur; it states that, and gives the CE-valid checks to run after the database container is restarted."
---

# Check the database after a restart

Last reviewed: 2026-11-11

**CE has no pooler; connect directly to `postgres-app`.** The Community Edition
compose runs **no** transaction-pooling proxy in front of Postgres — the API's
engine talks to the `postgres-app` service over the install's internal network,
with its own bounded connection pool. There is therefore no pooler container,
no pooler healthcheck, and no proxy that can cache a failed lookup of the
database name. The condition this page is named for **cannot occur on a CE
install**, and there is nothing to restart.

## 1. What to check instead (after a database restart)

Restarting the database container is still the one action in this window that
closes the API's database connections. On CE the only thing that can go wrong
afterwards is the database itself not coming back healthy. Check it in this order:

Set `MOD_API_BASE` to your install's API URL (the one you open the Dashboard on;
on a local CE install that is `http://localhost:8000`).

Run every `docker compose` command below from the checkout root (the directory with `docker-compose.yml` and `.env`).

```bash
docker compose ps postgres-app
                                  # expect: running, (healthy)
docker compose exec postgres-app pg_isready -p 5432
                                  # expect: "accepting connections"
curl -sk "$MOD_API_BASE/v1/health"
                                  # expect: 200, JSON liveness payload
```

`docker compose ps` / `docker compose exec` take the **service** name
(`postgres-app`), which is what you type in every command on this install —
never a container name.

If the database is healthy but the API is not answering, the fault is on the
API side, not a name-resolution strand: go to
[Restart the API without losing runs](/runbooks/api-restart-and-drain) and
drain before you restart it.

## 2. Do not do these

- Do not restart `postgres-app` to clear anything that is not a database fault
  — a database restart closes every client connection, the API's included.
- Do not delete or recreate the database volume to "clear" a bad state.
- Do not hard-stop the platform to work around a fault.

## Related

- [Restart the API without losing runs](/runbooks/api-restart-and-drain) —
  drain first when the API container also needs restarting.
- [First steps on an incident](/runbooks/incident-first-steps) —
  where to look when the fault is not DNS.
