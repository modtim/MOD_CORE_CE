# WebSearch — system web-search node (add-web-search-node).
#
# A reusable workflow node: it runs a keyless DuckDuckGo web search
# for QUERY, mines the results for domain jargon, candidate github
# repos and pypi packages (reusing the platform's web_expand
# extraction logic, vendored into this image), and returns a
# structured JSON artifact to the platform via an internal,
# service-account-authenticated callback.
#
# No Harbor / Vault / platform-DB credentials live in the container —
# the artifact is POSTed back through an internal API endpoint that
# the worker authenticates against using a service-account token
# passed via env vars (mod_node_auth).
#
# See: the internal design notes

FROM python:3.11-slim

WORKDIR /app

# Single dependency: requests for the DDG search + API round-trip.
RUN pip install --no-cache-dir requests==2.31.0

COPY entrypoint.py /app/entrypoint.py
COPY mod_node_auth.py /app/mod_node_auth.py
COPY web_expand.py /app/web_expand.py
RUN chmod +x /app/entrypoint.py

ENTRYPOINT ["python3", "/app/entrypoint.py"]
