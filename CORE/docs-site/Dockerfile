FROM node:20-alpine AS builder
WORKDIR /app
# GT.B1: no DASHBOARD_URL build arg — the build always bakes the
# __MOD_DASHBOARD_URL__ placeholder (see docusaurus.config.ts) and the
# container entrypoint rewrites it from $DASHBOARD_URL at start, so a
# domain change needs only a container recreate, not an image rebuild.
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/build /usr/share/nginx/html
# GY.C14 — RFC 9116 security.txt must be machine-readable on the docs host.
# The dot-directory is re-copied explicitly so a missing static/.well-known
# fails the build loudly instead of silently falling through to the SPA
# index page (and so a future `.*`-style ignore can never drop it).
COPY --from=builder /app/build/.well-known /usr/share/nginx/html/.well-known
# Custom nginx config — adds Access-Control-Allow-Origin to
# /help-tree.json so the Dashboard's Mind Map tab can fetch it
# cross-subdomain. Replaces the stock default.conf.
COPY nginx.conf /etc/nginx/conf.d/default.conf
# GT.B1: entrypoint rewrites the __MOD_DASHBOARD_URL__ placeholder from env
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
