FROM python:3.11-slim

RUN pip install --no-cache-dir requests==2.31.0

# Create application directories
RUN mkdir -p /app

# Copy scripts
COPY validate.py /app/validate.py
# worker-mediated-node-credentials: shared node-auth helper (vendored into the
# build context by the batch build script). /app is on sys.path when validate.py
# runs as /app/validate.py, so `import mod_node_auth` resolves.
COPY mod_node_auth.py /app/mod_node_auth.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Set working directory (same as all other containers)
WORKDIR /data

# Entrypoint - use shell form to match other containers
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]

