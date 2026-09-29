"""Narrow answer-model boundary for P3 (plan 5.9, decision D-028).

The literature domain must not know how providers are selected, configured or
authenticated: that stays in the existing Agent V2 provider factory.  This module
freezes only two things:

* :class:`ModelIdentity` -- the provider/model/configuration record every
  :class:`~src.literature.models.AnswerClaim` has to carry, hashed without any
  secret so a rotated API key never changes a recorded answer; and
* :class:`EvidenceAnswerModel` -- a two-member protocol (``identity`` +
  ``complete``) the service depends on and tests replace with a deterministic
  fake.

:class:`AgentProviderAnswerModel` adapts any existing provider object exposing
``chat(...)``.  It performs no provider selection, copies no credentials into the
identity, and never swallows a provider failure: deciding what a failure means is
the service's job, not the adapter's.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.literature.answer import ANSWER_PROMPT_VERSION
from src.literature.models import canonical_hash

ANSWER_TEMPERATURE = 0.0
#: Thinking models spend output budget on reasoning before the final JSON, so the
#: answer call needs room for both.  DeepSeek's JSON Output guide requires a
#: max_tokens that cannot truncate the JSON mid-way; 2048 truncated real answers
#: (``finish_reason=length`` with empty content).
DEFAULT_MAX_TOKENS = 8_192
#: DeepSeek-documented JSON Output mode; the prompt already contains the word
#: "json" and shows the expected object, as that guide requires.
JSON_RESPONSE_FORMAT = {"type": "json_object"}
_HEX_DIGITS = set("0123456789abcdef")


class ModelIdentity(BaseModel):
    """Provider, model and a secret-free hash of the answering configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=500)
    config_hash: str
    #: Thinking mode the endpoint actually receives, when the provider exposes one.
    #: ``None`` means the provider has no thinking control or sends no such field.
    thinking_mode: str | None = Field(default=None, max_length=32)
    #: False when the provider drops ``temperature`` (thinking mode does this on
    #: DeepSeek), so a recorded answer never claims sampling that did not happen.
    temperature_applied: bool = True

    @field_validator("provider", "model")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model identity fields cannot be blank")
        return normalized

    @field_validator("config_hash")
    @classmethod
    def validate_config_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in _HEX_DIGITS for char in normalized):
            raise ValueError("config_hash must be a 64-character hex digest")
        return normalized


@runtime_checkable
class EvidenceAnswerModel(Protocol):
    """Everything the answer service is allowed to assume about a model."""

    @property
    def identity(self) -> ModelIdentity: ...

    async def complete(self, *, system_prompt: str, prompt: str) -> str: ...


def effective_thinking_mode(provider: Any) -> str | None:
    """Report the thinking mode a provider will send, without changing it.

    The answer path must not silently override the user's model configuration, so
    this only *describes* what the provider is set to.  It matters for honesty:
    DeepSeek drops ``temperature`` once thinking is enabled, so an answer produced
    in thinking mode is not sampled at ``temperature=0``.
    """

    if not hasattr(provider, "thinking_mode"):
        return None
    configured = str(provider.thinking_mode)
    resolver = None
    try:
        from src.llm_request_policy import resolve_thinking_mode

        resolver = resolve_thinking_mode
    except ImportError:  # pragma: no cover - policy module is part of the app
        resolver = None
    if resolver is None:
        return configured
    base_url = str(getattr(provider, "base_url", "") or "")
    model = str(getattr(provider, "model", "") or "")
    resolved = resolver(base_url, model, configured)
    # ``None`` means "no vendor field is sent"; report the configured intent so a
    # reader can still tell an explicit choice from a default.
    return resolved if resolved is not None else configured


def build_model_identity(
    *,
    provider: str,
    model: str,
    base_url: str = "",
    temperature: float = ANSWER_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    prompt_version: str = ANSWER_PROMPT_VERSION,
    thinking_mode: str | None = None,
    temperature_applied: bool = True,
) -> ModelIdentity:
    """Hash the recorded answering configuration, excluding every secret."""

    normalized_provider = provider.strip()
    normalized_model = model.strip()
    return ModelIdentity(
        provider=normalized_provider,
        model=normalized_model,
        thinking_mode=thinking_mode,
        temperature_applied=bool(temperature_applied),
        config_hash=canonical_hash(
            {
                "provider": normalized_provider,
                "model": normalized_model,
                "base_url": base_url.strip(),
                "temperature": float(temperature),
                "temperature_applied": bool(temperature_applied),
                "max_tokens": int(max_tokens),
                "prompt_version": prompt_version,
                "thinking_mode": thinking_mode,
            }
        ),
    )


class AnswerModelError(RuntimeError):
    """The provider answered, but not with a usable structured result."""


class AgentProviderAnswerModel:
    """Adapter over an existing Agent provider; adds no provider selection."""

    def __init__(
        self,
        *,
        provider: Any,
        provider_name: str,
        model: str,
        base_url: str = "",
        temperature: float = ANSWER_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        prompt_version: str = ANSWER_PROMPT_VERSION,
        thinking_mode: str | None = None,
    ) -> None:
        if not callable(getattr(provider, "chat", None)):
            raise TypeError("answer provider must expose an async chat(...) method")
        self._provider = provider
        self._temperature = float(temperature)
        self._max_tokens = int(max_tokens)
        self._request_json = _accepts_response_format(provider)
        thinking = thinking_mode if thinking_mode is not None else effective_thinking_mode(provider)
        self._identity = build_model_identity(
            provider=provider_name,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_version=prompt_version,
            thinking_mode=thinking,
            # Thinking mode makes DeepSeek drop temperature, so record that the
            # configured sampling value was not applied rather than implying it was.
            temperature_applied=thinking != "enabled",
        )

    @property
    def identity(self) -> ModelIdentity:
        return self._identity

    async def complete(self, *, system_prompt: str, prompt: str) -> str:
        from src.agent_v2.types import Message, MessageRole, TextBlock

        kwargs: dict[str, Any] = {}
        if self._request_json:
            kwargs["response_format"] = JSON_RESPONSE_FORMAT
        response = await self._provider.chat(
            [Message(role=MessageRole.USER, blocks=[TextBlock(text=prompt)])],
            tools=None,
            system_prompt=system_prompt,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            **kwargs,
        )
        text = response.text_content()
        if not text.strip():
            # Distinguish "the model never produced an answer" from "the model
            # produced prose we refuse"; ``length`` means the budget ran out, which
            # is a configuration failure, not a model disagreement.
            stop_reason = str(getattr(response, "stop_reason", "") or "")
            raise AnswerModelError(
                f"empty structured response (stop_reason={stop_reason or 'unknown'})"
            )
        return text


def _accepts_response_format(provider: Any) -> bool:
    """Whether the provider's ``chat`` accepts ``response_format``."""

    try:
        import inspect

        parameters = inspect.signature(provider.chat).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    if "response_format" in parameters:
        return True
    return any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())


__all__ = [
    "ANSWER_TEMPERATURE",
    "DEFAULT_MAX_TOKENS",
    "JSON_RESPONSE_FORMAT",
    "AgentProviderAnswerModel",
    "AnswerModelError",
    "EvidenceAnswerModel",
    "ModelIdentity",
    "build_model_identity",
    "effective_thinking_mode",
]
