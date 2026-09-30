"""Single place to configure the LLM provider/model for the whole worker.

Every agent, subagent and the orchestrator build their model through
``get_llm(temperature)`` so switching provider/model is a one-line change
(either the defaults below or the ``LLM_PROVIDER`` / ``LLM_MODEL`` env vars).

Examples::

    LLM_PROVIDER="google"       # Gemini (GEMINI_API_KEY)
    LLM_PROVIDER="groq"         # free tier, no card (GROQ_API_KEY)
    LLM_PROVIDER="openrouter"   # free ":free" models (OPENROUTER_API_KEY)
    LLM_PROVIDER="mistral"      # La Plateforme (MISTRAL_API_KEY)
    LLM_PROVIDER="ollama"       # local, unlimited, no key (http://localhost:11434)

Every provider except ``google`` is an OpenAI-compatible endpoint, so any
other service (Cerebras, Together, a corporate proxy, ...) works by keeping
``LLM_PROVIDER="openai"`` and setting ``LLM_BASE_URL`` + ``OPENAI_API_KEY``.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from langchain_core.language_models.chat_models import BaseChatModel

# provider -> config: ``key`` names the env var holding the API key, ``model``
# is the default when LLM_MODEL is unset, ``base_url`` (optional) routes the
# OpenAI-compatible client at that endpoint instead of api.openai.com.
PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {"key": "OPENAI_API_KEY", "model": "gpt-4o-mini"},
    "google": {"key": "GEMINI_API_KEY", "model": "gemini-3.6-flash"},
    "groq": {
        "key": "GROQ_API_KEY",
        "model": "openai/gpt-oss-120b",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "openrouter": {
        "key": "OPENROUTER_API_KEY",
        "model": "qwen/qwen3.8-27b:free",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "mistral": {
        "key": "MISTRAL_API_KEY",
        "model": "mistral-small-latest",
        "base_url": "https://api.mistral.ai/v1",
    },
    "ollama": {
        "key": "OLLAMA_API_KEY",
        "model": "llama3.3",
        "base_url": "http://localhost:11434/v1",
    },
}

DEFAULT_PROVIDER = "openai"

# Hard per-call cap. Without it a stalled provider connection hangs the job
# for the SDK's default (up to 10 minutes), which leaves the session stuck
# "PENDING" with a typing indicator the user can't get rid of.
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT") or 60)

# Providers served by langchain-openai against an (possibly non-OpenAI) base URL.
_OPENAI_COMPAT = frozenset({"openai", "groq", "openrouter", "mistral", "ollama"})


def provider_name() -> str:
    name = (os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if name not in PROVIDERS:
        raise ValueError(
            f"Unknown LLM_PROVIDER {name!r}; expected one of {sorted(PROVIDERS)}"
        )
    return name


def model_name() -> str:
    configured = (os.environ.get("LLM_MODEL") or "").strip()
    if configured:
        return configured
    return PROVIDERS[provider_name()]["model"]


def get_llm(temperature: float = 0.2) -> BaseChatModel:
    """Build the shared chat model for one call site.

    The model/provider is process-wide; only ``temperature`` varies per site.
    """
    provider = provider_name()
    model = model_name()
    cfg = PROVIDERS[provider]

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model, temperature=temperature, timeout=LLM_TIMEOUT
        )

    if provider in _OPENAI_COMPAT:
        from langchain_openai import ChatOpenAI

        base_url = (os.environ.get("LLM_BASE_URL") or "").strip() or cfg.get(
            "base_url"
        )
        api_key = (os.environ.get(cfg["key"]) or "").strip()
        if not api_key:
            if provider == "ollama":
                # Local server ignores the Authorization header entirely.
                api_key = "ollama"
            else:
                raise ValueError(
                    f"LLM_PROVIDER={provider!r} requires {cfg['key']} to be set in .env"
                )
        kwargs: dict = {
            "model": model,
            "temperature": temperature,
            "api_key": api_key,
            "timeout": LLM_TIMEOUT,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    raise ValueError(f"Unsupported LLM provider {provider!r}")
