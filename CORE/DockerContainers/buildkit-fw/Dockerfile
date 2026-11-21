# buildkit-fw: owns the network namespace of the rootless BuildKit sidecar and
# applies the "no LAN" egress policy to it (see docs/editions/ce-cut-audit/node-builder-buildkit.md).
# The build RUN steps run inside buildkitd's own netns (--oci-worker-net=host
# in rootless mode), so a filter in THAT netns is a filter on every LLM-written
# Dockerfile. buildkitd runs without CAP_NET_ADMIN and cannot change it.
FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f
RUN apk add --no-cache iptables ip6tables
COPY fw.sh /usr/local/bin/fw.sh
RUN chmod 0555 /usr/local/bin/fw.sh
ENTRYPOINT ["/usr/local/bin/fw.sh"]
