"""
Multi-LLM provider abstraction layer.

Normalizes streaming from different LLM providers into a common
async generator interface so the rest of the app doesn't care
which provider is being used.

Supported providers:
  - openai          (GPT-4o, GPT-4o-mini, etc.)
  - anthropic       (Claude Sonnet, Opus, Haiku)
  - google          (Gemini 2.0 Flash, Pro, etc.)
  - ollama          (any local model)
  - groq            (Llama, Mixtral via Groq cloud)
  - mistral         (Mistral models)
  - deepseek        (DeepSeek Coder, Chat)
  - openai_compatible (vLLM, llama.cpp, LM Studio, TGI, etc.)

Configuration via environment variables:
  LLM_PROVIDER    - Provider name (default: openai)
  LLM_MODEL       - Model identifier (default: gpt-4o-mini)
  LLM_API_KEY     - API key for the provider
  LLM_BASE_URL    - Base URL (required for ollama, openai_compatible)
  LLM_TEMPERATURE - Generation temperature (default: 0.3)
  LLM_MAX_TOKENS  - Max output tokens (default: 4096)
"""

import os
import json
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """Configuration for an LLM provider."""
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: Optional[str] = None
    temperature: float = 0.3
    max_tokens: int = 4096
    keep_alive: Optional[str] = None  # Ollama only: "-1" (forever), "5m", "0" (unload), etc.
    num_ctx: Optional[int] = None     # Ollama only: caps the KV-cache context window.

    @classmethod
    def from_env(cls) -> "LLMConfig":
        keep_alive = os.getenv("LLM_KEEP_ALIVE", "").strip()
        num_ctx_raw = os.getenv("LLM_NUM_CTX", "").strip()
        num_ctx: Optional[int] = None
        if num_ctx_raw:
            try:
                num_ctx = int(num_ctx_raw)
            except ValueError:
                num_ctx = None
        return cls(
            provider=os.getenv("LLM_PROVIDER", "openai").lower().strip(),
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            base_url=os.getenv("LLM_BASE_URL"),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.3")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
            keep_alive=keep_alive or None,
            num_ctx=num_ctx,
        )

    def validate(self):
        """Raise if config is invalid."""
        # Keyless is legitimate for a self-hosted server: Ollama, and an
        # openai_compatible endpoint (vLLM / llama.cpp / LM Studio / a MOD
        # Persistent Stack), which require LLM_BASE_URL (checked below).
        no_key_providers = {"ollama", "openai_compatible"}
        if self.provider not in no_key_providers and not self.api_key:
            raise ValueError(
                f"LLM_API_KEY is required for provider '{self.provider}'. "
                f"Set it via environment variable."
            )
        if self.provider in {"ollama", "openai_compatible"} and not self.base_url:
            raise ValueError(
                f"LLM_BASE_URL is required for provider '{self.provider}'. "
                f"Example: http://localhost:11434 for Ollama."
            )


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Abstract base for all LLM providers."""

    def __init__(self, config: LLMConfig):
        self.config = config

    @abstractmethod
    async def stream(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> AsyncIterator[str]:
        """Yield content chunks as they arrive from the LLM."""
        ...

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        **_ignored: object,
    ) -> str:
        """Convenience: collect the full response into a string.

        Accepts and silently ignores unknown kwargs (e.g.
        ``response_format='json'``) so older callers that pass
        provider-specific options don't blow up with a TypeError.
        Request-shaping knobs that a specific provider can honour
        should be added to the provider subclass's ``stream`` method
        directly, not here.
        """
        parts = []
        async for chunk in self.stream(system_prompt, user_prompt):
            parts.append(chunk)
        return "".join(parts)


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def _openai_base_url(raw: Optional[str]) -> Optional[str]:
    """Normalize LLM_BASE_URL for the AsyncOpenAI client.

    The OpenAI Python client expects base_url to include `/v1` (it
    appends `/chat/completions` directly). The API's own
    `utils/llm_client.py` uses the opposite convention — it expects
    a root URL and appends `/v1/chat/completions` itself — and the
    platform ai_config publishes LLM_BASE_URL in that root-only form
    (`http://ollama:11434`).

    Without this normalizer, the Containerizer hits
    `http://ollama:11434/chat/completions` and gets a 404 from vLLM
    (which only serves `/v1/*`). Manual-test repro 2026-05-22.

    Idempotent: passes through unchanged when the URL already ends
    in `/v1` (so explicit user configs keep working).
    """
    if not raw:
        return None
    url = raw.rstrip("/")
    if url.endswith("/v1") or "/v1/" in url:
        return url
    return f"{url}/v1"


class OpenAIProvider(LLMProvider):
    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        from openai import AsyncOpenAI

        base_url = _openai_base_url(self.config.base_url)
        client = AsyncOpenAI(
            api_key=self.config.api_key,
            **({"base_url": base_url} if base_url else {}),
        )
        response = await client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta is not None:
                yield delta


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider):
    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=self.config.api_key)
        async with client.messages.stream(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            async for text in stream.text_stream:
                yield text


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

class GoogleProvider(LLMProvider):
    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        from google import genai

        client = genai.Client(api_key=self.config.api_key)

        combined_prompt = f"{system_prompt}\n\n{user_prompt}"

        async for chunk in client.aio.models.generate_content_stream(
            model=self.config.model,
            contents=combined_prompt,
        ):
            if chunk.text:
                yield chunk.text


# ---------------------------------------------------------------------------
# Ollama (local models — uses the OpenAI-compatible API)
# ---------------------------------------------------------------------------

class OllamaProvider(LLMProvider):
    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        import httpx

        base = (self.config.base_url or "http://localhost:11434").rstrip("/")
        url = f"{base}/api/chat"

        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": True,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        if self.config.num_ctx:
            payload["options"]["num_ctx"] = self.config.num_ctx
        if self.config.keep_alive:
            # Ollama accepts either a duration string ("5m", "1h") or a numeric
            # second count. Negative means "pin forever". "-1" as a JSON string
            # fails Ollama's time.ParseDuration; convert to a JSON number.
            try:
                payload["keep_alive"] = int(self.config.keep_alive)
            except ValueError:
                payload["keep_alive"] = self.config.keep_alive

        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        content = data.get("message", {}).get("content", "")
                        if content:
                            yield content
                        if data.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue


# ---------------------------------------------------------------------------
# OpenAI-compatible (vLLM, llama.cpp, LM Studio, TGI, etc.)
# ---------------------------------------------------------------------------

class OpenAICompatibleProvider(OpenAIProvider):
    """Same as OpenAI but requires a custom base_url."""
    pass


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

class GroqProvider(LLMProvider):
    """Groq uses the OpenAI-compatible API format."""
    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self.config.api_key,
            base_url="https://api.groq.com/openai/v1",
        )
        response = await client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta is not None:
                yield delta


# ---------------------------------------------------------------------------
# Mistral
# ---------------------------------------------------------------------------

class MistralProvider(LLMProvider):
    """Mistral uses the OpenAI-compatible API format."""
    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self.config.api_key,
            base_url="https://api.mistral.ai/v1",
        )
        response = await client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta is not None:
                yield delta


# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------

class DeepSeekProvider(LLMProvider):
    """DeepSeek uses the OpenAI-compatible API format."""
    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self.config.api_key,
            base_url="https://api.deepseek.com",
        )
        response = await client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta is not None:
                yield delta


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "ollama": OllamaProvider,
    "groq": GroqProvider,
    "mistral": MistralProvider,
    "deepseek": DeepSeekProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def get_provider(config: Optional[LLMConfig] = None) -> LLMProvider:
    """
    Create an LLM provider instance from config (or env vars if no config given).

    Usage:
        provider = get_provider()
        async for chunk in provider.stream(system_prompt, user_prompt):
            print(chunk, end="")
    """
    if config is None:
        config = LLMConfig.from_env()

    config.validate()

    cls = PROVIDERS.get(config.provider)
    if cls is None:
        supported = ", ".join(sorted(PROVIDERS.keys()))
        raise ValueError(
            f"Unknown LLM provider '{config.provider}'. "
            f"Supported: {supported}"
        )

    logger.info(f"Using LLM provider: {config.provider} / {config.model}")
    return cls(config)
