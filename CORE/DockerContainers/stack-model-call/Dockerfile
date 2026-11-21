# stack-model-call node — N image cogs + PROMPT / SYSTEM_PROMPT / JSON_SCHEMA
# -> the tenant's own model stack via POST {API_URL}/v1/llm/chat/completions.
# stdlib + requests only (requests is installed here; nothing else is fetched).
# No ENV/ARG carries a credential: the run_task token comes from mod_node_auth
# at run time, and API_URL is injected by the worker, never baked here.
FROM python:3.11-slim
RUN pip install --no-cache-dir requests==2.31.0
RUN mkdir -p /data/input /data/output
RUN useradd -m -u 10001 modnode && chown -R 10001:10001 /data
WORKDIR /data
COPY entrypoint.py /app/entrypoint.py
COPY mod_node_auth.py /app/mod_node_auth.py
USER modnode
ENTRYPOINT ["python3", "/app/entrypoint.py"]
