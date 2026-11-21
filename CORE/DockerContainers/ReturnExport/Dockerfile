FROM python:3.11-slim

RUN pip install --no-cache-dir requests

# Create application directories
RUN mkdir -p /app

# Copy scripts
COPY export.py /app/export.py
COPY mod_node_auth.py /app/mod_node_auth.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Set working directory (same as all other containers)
WORKDIR /data

# Entrypoint - use shell form to match other containers
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]

