"""The advisor client — LiteLLM in, validated JSON out (05-advisor-prompts.md §2).

This module is the only thing in kvseo that talks to an LLM, and it never
imports a provider SDK: all calls go through LiteLLM's ``acompletion`` so the
operator can point kvseo at Anthropic, OpenAI, Gemini, or a local Ollama with a
one-line config change (ADR-002). The call is injectable (``completion=``) so
tests exercise the parse / retry / persist machinery without a network or a key.

Contract per 05 §6.3:

* validate the response against the pydantic schema;
* on failure, retry exactly once with the validation error appended;
* on a second failure, persist an ``invalid_output`` row and return it;
* on a provider/transport error, persist a ``failed`` row and return it;
* on success, persist the validated output plus token usage and cost.

Every run is written to ``advisor_outputs`` keyed to its ``audit_run_id`` — the
audit data is untouched whatever the advisor does.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.config.secrets import get_secret
from kvseo.config.settings import Settings
from kvseo.core.advisor.context import AdvisorError, Context, build_context
from kvseo.core.advisor.prompts import RETRY_SUFFIX, SYSTEM_PRIORITIZE
from kvseo.core.advisor.schemas import PrioritizationOutput
from kvseo.storage.models import AdvisorOutput as AdvisorOutputORM

# An async chat-completion callable shaped like ``litellm.acompletion``.
CompletionFn = Callable[..., Awaitable[Any]]

# Conservative user-message token ceiling (05 §3). The gate is a char-based
# estimate — deliberately cheap and import-free; the *actual* token counts that
# drive cost are read back from the response usage after the call.
_CONTEXT_TOKEN_BUDGET = 8000
_CHARS_PER_TOKEN = 4

# LiteLLM provider prefix -> the env var it reads the key from. We export the
# key from the OS keyring into this var before the call (05 §2).
_PROVIDER_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "azure": "AZURE_API_KEY",
    "cohere": "COHERE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}
_KEYLESS_PROVIDERS = {"ollama"}  # local — no API key needed


class ContextOverflowError(AdvisorError):
    """The assembled context exceeds the token budget — rebuild it smaller
    rather than letting the call truncate silently (05 §3)."""


class AdvisorRun(BaseModel):
    """The persisted outcome of one advisor call (mirrors an ``advisor_outputs``
    row). ``output`` is present only when ``status == 'success'``."""

    id: uuid.UUID
    audit_run_id: uuid.UUID
    prompt_id: str
    provider: str
    model: str
    status: str  # 'success' | 'invalid_output' | 'failed'
    output: PrioritizationOutput | None = None
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_usd: float | None = None
    duration_ms: int | None = None


async def prioritize(
    audit_id: uuid.UUID,
    *,
    engine: Engine,
    settings: Settings | None = None,
    completion: CompletionFn | None = None,
) -> AdvisorRun:
    """Run the prioritization advisor against a completed audit.

    Raises :class:`AdvisorError` if the audit isn't ready or no provider key is
    available (a pre-flight reject, no DB row). Any failure *after* the call is
    recorded as a row and returned, not raised — the caller inspects ``status``.
    """
    settings = settings or Settings()
    provider = settings.advisor.provider
    model = settings.advisor.model

    context = build_context(audit_id, engine)
    user = user_message(context)
    _check_budget(user)
    complete = completion or _litellm_acompletion
    if completion is None:
        _ensure_provider_key(provider)  # only gate real calls; tests inject

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PRIORITIZE},
        {"role": "user", "content": user},
    ]
    kwargs = _completion_kwargs(provider, settings)
    full_model = f"{provider}/{model}"

    started = time.monotonic()
    last_raw: str | None = None
    last_error: str | None = None
    last_usage: tuple[int | None, int | None] = (None, None)
    last_cost: float | None = None

    for attempt in (1, 2):
        try:
            response = await complete(model=full_model, messages=messages, **kwargs)
        except Exception as exc:  # provider / transport error — not retryable here
            return _record(
                engine, audit_id, provider, model, status="failed",
                error=f"{type(exc).__name__}: {exc}", duration_ms=_elapsed_ms(started),
            )

        last_raw = _content(response)
        last_usage = _usage(response)
        last_cost = _cost(response)
        try:
            output = PrioritizationOutput.model_validate_json(last_raw or "")
        except ValidationError as exc:
            last_error = _one_line(exc)
            if attempt == 1:
                messages = [
                    *messages,
                    {"role": "assistant", "content": last_raw or ""},
                    {"role": "user", "content": RETRY_SUFFIX.strip()},
                ]
                continue
            break  # second failure — fall through to the invalid_output record
        return _record(
            engine, audit_id, provider, model, status="success", output=output,
            raw=last_raw, usage=last_usage, cost=last_cost, duration_ms=_elapsed_ms(started),
        )

    return _record(
        engine, audit_id, provider, model, status="invalid_output",
        raw=last_raw, error=last_error, usage=last_usage, cost=last_cost,
        duration_ms=_elapsed_ms(started),
    )


def user_message(context: Context) -> str:
    """Serialize the context to the JSON the model reads. Pretty-printed so the
    model can index 'gsc.queries[3]'-style references reliably; the token cost of
    whitespace is negligible against an 8k budget."""
    return json.dumps(context.model_dump(mode="json"), indent=2, ensure_ascii=False)


# --- LLM plumbing --------------------------------------------------------


async def _litellm_acompletion(**kwargs: Any) -> Any:
    # Imported lazily: litellm is a heavy import, and tests inject their own
    # completion so they never pay it.
    import litellm

    return await litellm.acompletion(**kwargs)


def _completion_kwargs(provider: str, settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "max_tokens": settings.advisor.max_tokens,
        "temperature": settings.advisor.temperature,
    }
    # JSON mode where the provider supports it. Ollama uses a different switch
    # and chokes on response_format, so we omit it there (_normalize, 05 §9).
    if provider not in _KEYLESS_PROVIDERS:
        kwargs["response_format"] = {"type": "json_object"}
    if settings.advisor.cache_enabled:
        kwargs["caching"] = True
    return kwargs


def _ensure_provider_key(provider: str) -> None:
    if provider in _KEYLESS_PROVIDERS:
        return
    env_var = _PROVIDER_ENV.get(provider)
    if env_var is None:
        return  # unknown provider — let LiteLLM resolve its own credentials
    if os.environ.get(env_var):
        return
    key = get_secret(f"{provider}:api_key")
    if key:
        os.environ[env_var] = key
        return
    raise AdvisorError(
        f"no API key for advisor provider '{provider}'. Set ${env_var} in your "
        f"environment (or store it in your keyring as '{provider}:api_key'), then retry."
    )


def _check_budget(user: str) -> None:
    estimate = len(user) // _CHARS_PER_TOKEN
    if estimate > _CONTEXT_TOKEN_BUDGET:
        raise ContextOverflowError(
            f"context is ~{estimate} tokens, over the {_CONTEXT_TOKEN_BUDGET}-token budget. "
            "Narrow the audit (fewer GSC queries) and retry."
        )


def _content(response: Any) -> str | None:
    try:
        return response.choices[0].message.content  # type: ignore[no-any-return]
    except (AttributeError, IndexError, KeyError):
        return None


def _usage(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return (None, None)
    return (getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None))


def _cost(response: Any) -> float | None:
    # LiteLLM stamps the computed USD cost here; reading it avoids importing
    # litellm just to price the call (and is None for injected test responses).
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, dict) and hidden.get("response_cost") is not None:
        try:
            return float(hidden["response_cost"])
        except (TypeError, ValueError):
            return None
    return None


# --- Persistence ---------------------------------------------------------


def _record(
    engine: Engine,
    audit_id: uuid.UUID,
    provider: str,
    model: str,
    *,
    status: str,
    output: PrioritizationOutput | None = None,
    raw: str | None = None,
    error: str | None = None,
    usage: tuple[int | None, int | None] = (None, None),
    cost: float | None = None,
    duration_ms: int | None = None,
) -> AdvisorRun:
    run_id = uuid.uuid4()
    prompt_tokens, completion_tokens = usage
    with Session(engine) as session:
        session.add(
            AdvisorOutputORM(
                id=run_id,
                audit_run_id=audit_id,
                prompt_id="prioritize",
                provider=provider,
                model=model,
                status=status,
                output=output.model_dump(mode="json") if output is not None else None,
                raw_response=raw,
                error=error,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                duration_ms=duration_ms,
            )
        )
        session.commit()
    return AdvisorRun(
        id=run_id,
        audit_run_id=audit_id,
        prompt_id="prioritize",
        provider=provider,
        model=model,
        status=status,
        output=output,
        error=error,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=cost,
        duration_ms=duration_ms,
    )


def latest_run(audit_id: uuid.UUID, engine: Engine) -> AdvisorRun | None:
    """The most recent prioritization run for an audit, or None if never run.

    Used by ``kvseo advisor show`` and the report renderer to read a stored
    recommendation without re-calling the model."""
    with Session(engine) as session:
        row = session.scalars(
            select(AdvisorOutputORM)
            .where(
                AdvisorOutputORM.audit_run_id == audit_id,
                AdvisorOutputORM.prompt_id == "prioritize",
            )
            .order_by(AdvisorOutputORM.created_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        output = (
            PrioritizationOutput.model_validate(row.output)
            if row.status == "success" and row.output is not None
            else None
        )
        return AdvisorRun(
            id=row.id,
            audit_run_id=row.audit_run_id,
            prompt_id=row.prompt_id,
            provider=row.provider,
            model=row.model,
            status=row.status,
            output=output,
            error=row.error,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            estimated_cost_usd=row.estimated_cost_usd,
            duration_ms=row.duration_ms,
        )


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())[:500]
