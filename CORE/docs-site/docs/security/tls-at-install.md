---
sidebar_position: 31
slug: /security/tls-at-install
title: Stack ingress TLS (the stack-ingress edge)
description: The optional stack-ingress Caddy edge terminates TLS for stack hostnames. It is off by default and turns on only when STACK_TLS_ISSUER is set to letsencrypt or vault-acme.
---

# Stack ingress TLS (the stack-ingress edge)

Apps and services that you run as stacks are reached through a hostname under
the stack wildcards (`*.apps.<base domain>` and `*.services.<base domain>`) or a
tenant custom domain. The **stack-ingress edge** is an optional Caddy server that
terminates TLS (handles the encrypted connection) for those hostnames and forwards
them to the stack.

> **In plain words:** the edge is off until you turn it on. When it is on, it
> serves a certificate for each stack hostname and nothing else. It does not
> serve the platform web pages.

## Turning it on

The edge is a Compose profile named `stack-ingress`. It does not start unless the
profile is enabled, and it does nothing until an issuer is set:

- `STACK_TLS_ISSUER` unset, or set to any other value: the edge stays **inert**.
  It opens no 443 listener and requests no certificates.
- `STACK_TLS_ISSUER=letsencrypt`: certificates come from the public Let's Encrypt
  ACME directory. A certificate is requested the first time a hostname is used,
  and only for hostnames the platform authorizes.
- `STACK_TLS_ISSUER=vault-acme`: certificates come from Vault PKI through its ACME
  endpoint. It is optional: `STACK_TLS_VAULT_ACME_DIRECTORY` defaults to the PKI ACME path under `STACK_TLS_VAULT_ADDR`; set it only to override. This suits
  air-gapped sites that run their own CA.

## Ports

The edge publishes two host ports by default:

| Host port (default) | Variable | Container port |
|---|---|---|
| `8880` | `STACK_INGRESS_HTTP_PORT` | `80` |
| `8443` | `STACK_INGRESS_HTTPS_PORT` | `443` |

The edge does not publish host `80` or `443`. If clients reach the stack
hostnames on the standard ports, your own network setup must forward ports
`80` and `443` to `8880` and `8443`. This repository does not configure that
forwarding.

## What the edge serves

- Only stack hostnames: the `*.apps` and `*.services` wildcards, plus tenant
  custom domains that the platform authorizes. The platform authorizes a
  hostname through an internal ask endpoint before any certificate is issued.
- Platform web pages are not served by the edge. They are served by the
  platform's own web tier, which this page does not describe.
- The Caddy admin API listens on the loopback address `127.0.0.1:2019` only and
  is not published to any host port.

## Files

- `docker-compose.yml`: the `caddy` service (profile `stack-ingress`; image, ports, volumes).
- `CORE/startup/Caddyfile.template`: the edge configuration template.
- `CORE/startup/scripts/caddy-entrypoint.sh`: renders the template from
  `STACK_TLS_ISSUER` and starts Caddy. With an unset or invalid issuer it
  renders the inert configuration.
- The `caddy-data` and `caddy-config` volumes hold issued certificates and the
  Caddy configuration.

## Which setting to choose

Stack hostnames reachable over the internet with a public domain: use
`letsencrypt`. Air-gapped or LAN-only sites that run their own CA: use
`vault-acme`. Anything else: leave the edge off.

## FIPS

The Community Edition ships no FIPS-mode page, so what MOD does and does not do toward a
FIPS-validated crypto path is not documented in Community Edition.
