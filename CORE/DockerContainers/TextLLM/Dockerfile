# Text LLM node — prompt-composer / general text step.
# Calls your configured text LLM (OpenAI-compatible endpoint) resolved by role,
# on the platform network every general node already gets. Pure stdlib, no pip
# layer (mirrors the llm-call primitive), so the image stays tiny.
FROM python:3.11-slim
RUN mkdir -p /data/input /data/output
WORKDIR /data
COPY entrypoint.py /app/entrypoint.py
ENTRYPOINT ["python3", "/app/entrypoint.py"]
