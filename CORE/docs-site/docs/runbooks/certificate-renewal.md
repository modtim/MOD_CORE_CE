---
sidebar_position: 55
slug: /runbooks/certificate-renewal
title: Renew stack-ingress TLS certificates
description: "Runbook: check and renew the certificates the optional stack-ingress edge serves for stack hostnames, for each STACK_TLS_ISSUER setting."
---

# Renew stack-ingress TLS certificates

Last reviewed: 2026-11-11

This runbook covers one TLS edge: the optional stack-ingress Caddy edge
(container `mod_caddy_ingress`). It serves certificates for stack hostnames
only. For the install-time choice of issuer, ports and files, read
[Stack ingress TLS](/security/tls-at-install) first.

> **CE note:** MOD Community Edition has one TLS edge, the stack-ingress edge.
> It is off unless you enable its `stack-ingress` profile, and it issues
> nothing until `STACK_TLS_ISSUER` is set to `letsencrypt` or `vault-acme`.
> The steps below apply to that edge only.

## When to use

- A stack hostname shows an expired or soon-to-expire certificate, or a
  browser reports `ERR_CERT_DATE_INVALID`.
- The edge logs show a failed issuance or a hostname that is not served.

## Which issuer you run

| `STACK_TLS_ISSUER` | Who issues the certificate | Who renews it |
|---|---|---|
| `letsencrypt` | Public Let's Encrypt ACME, requested on first use of each hostname the platform authorizes (`caddy-entrypoint.sh:395-417`) | Caddy, while the edge runs (Caddy default; the CE files set no renewal option) |
| `vault-acme` | Vault PKI ACME endpoint. Default `http://openbao:8200/v1/pki/acme/directory` (the `STACK_TLS_VAULT_ADDR` and `STACK_TLS_VAULT_PKI_MOUNT` defaults, joined by `caddy-entrypoint.sh:252-253`) | Caddy, while the edge runs (Caddy default) |
| unset, or any other value | Nobody. The edge stays inert (`caddy-entrypoint.sh:469-477`) | Not applicable |

## Check the edge

1. Confirm the edge runs and which issuer it chose:
   ```
   docker compose --profile stack-ingress ps caddy
   docker logs mod_caddy_ingress 2>&1 | grep -E "Issuer:|INERT|reloaded" | tail -5
   ```
   Expect `Issuer: letsencrypt` or `Issuer: vault-acme (...)`.
   `INERT` means the issuer is unset or invalid. The service is `caddy`,
   gated by the `stack-ingress` profile, and the
   container is `mod_caddy_ingress`. Log lines:
   `caddy-entrypoint.sh:398, 422, 448, 471, 477, 552`.

2. Read the expiry of the certificate the edge serves for one stack hostname.
   Use the host port you forward to; the default HTTPS host port is `8443`
   (the `443` port of the `caddy` service, published through the variable `STACK_INGRESS_HTTPS_PORT`):
   ```
   openssl s_client -connect <host>:8443 -servername <stack-hostname> </dev/null 2>/dev/null | openssl x509 -noout -enddate
   ```
   Compare dates only. Never paste certificate or key material into a ticket,
   chat, log or commit.

3. Confirm the platform authorizes the hostname. Before issuing, the edge asks
   `STACK_TLS_ASK_URL` (default `http://api:80/v1/stacks/ingress/tls-authorize`,
   set by the `STACK_TLS_ASK_URL` variable of the `caddy` service). The ask call carries `MOD_INGRESS_ASK_TOKEN`
   when that variable is set (`caddy-entrypoint.sh:54-63`). A hostname the
   platform refuses gets no certificate (`caddy-entrypoint.sh:400-402`).

## Renew: letsencrypt and vault-acme

Nothing needs to run for a normal renewal. Caddy renews the certificates it
holds while the edge is running (Caddy default behaviour; the CE files set no
renewal option). Certificates are stored in the `caddy-data` volume (declared in the top-level
`volumes:` block), mounted at `/data` by the `caddy` service.

To make the edge re-read its state, restart only the edge. The API is not
touched:

```
docker compose --profile stack-ingress restart caddy
```

Do not delete the `caddy-data` volume to force a renewal. It holds the stored
certificates.

## Verify

- The `openssl` command in step 2 prints a later `notAfter` date than before.
- A request to the stack hostname shows a verified chain:
  `curl -v https://<stack-hostname>:8443/` reports `SSL certificate verify ok`.
  If the chain fails with `vault-acme`, the Vault PKI root is not in the
  client's trust store.
- The edge does not serve the platform web pages (`tls-at-install.md:56-57`),
  so do not verify the Dashboard or Workflow Builder through the edge.

If verification fails, stop and read the edge logs from step 1. Do not
improvise.

## Rollback

- Set `STACK_TLS_ISSUER` back to the previous value (the `STACK_TLS_ISSUER`
  variable of the `caddy` service) and recreate the edge so it reads the new
  value. A plain restart does not re-read the environment:
  `docker compose --profile stack-ingress up -d caddy`
  (the same form as the usage comment above the `caddy` service block).
- Certificates are kept in the `caddy-data` volume.
  Do not remove that volume as a rollback step.

Not a rollback: restarting the API. The edge is a separate container.

## Related

- [Stack ingress TLS](/security/tls-at-install): issuer choice, ports
  8880 and 8443, and the files the edge uses.
- DNS and certificate records runbook: planned.
