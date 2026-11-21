# MOD MCP fleet gateway image.
# NOTE: build context must be CORE/ (this Dockerfile COPYs sibling *-mcp packages).
#   docker build -f mcp-gateway/Dockerfile -t mod-mcp-gateway .
FROM python:3.11-slim

WORKDIR /app

# Hash-locked install. requirements.lock.txt is generated from
# requirements.txt INSIDE the running image (see its header + scripts/regen_python_locks.sh)
# and pins the exact wheels currently deployed, each with a sha256, so a rebuild
# can't silently swap in a yanked/typosquatted wheel. requirements.txt stays as
# the human-edited source (it is copied too so the regen has it in the image).
COPY mcp-gateway/requirements.txt /app/requirements.txt
COPY mcp-gateway/requirements.lock.txt /app/requirements.lock.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /app/requirements.lock.txt

# The six fleet module packages, flattened to top-level importable modules.
COPY node-wizard-mcp/node_wizard_mcp        /app/node_wizard_mcp
COPY workflow-runner-mcp/workflow_runner_mcp /app/workflow_runner_mcp
COPY automations-mcp/automations_mcp        /app/automations_mcp
COPY workflow-wizard-mcp/workflow_wizard_mcp /app/workflow_wizard_mcp
COPY project-wizard-mcp/project_wizard_mcp  /app/project_wizard_mcp
COPY stack-wizard-mcp/stack_wizard_mcp      /app/stack_wizard_mcp
COPY mcp-gateway/mcp_gateway                /app/mcp_gateway

ENV PYTHONPATH=/app \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

RUN useradd -m mcp && chown -R mcp /app
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)"

CMD ["uvicorn", "mcp_gateway.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
