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
DEFAULT_MAX_TOKENS = 2_048
_HEX_DIGITS = set("0123456789abcdef")


class ModelIdentity(BaseModel):
    """Provider, model and a secret-free hash of the answering configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=500)
    config_hash: str

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


def build_model_identity(
    *,
    provider: str,
    model: str,
    base_url: str = "",
    temperature: float = ANSWER_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    prompt_version: str = ANSWER_PROMPT_VERSION,
) -> ModelIdentity:
    """Hash the recorded answering configuration, excluding every secret."""

    normalized_provider = provider.strip()
    normalized_model = model.strip()
    return ModelIdentity(
        provider=normalized_provider,
        model=normalized_model,
        config_hash=canonical_hash(
            {
                "provider": normalized_provider,
                "model": normalized_model,
                "base_url": base_url.strip(),
                "temperature": float(temperature),
                "max_tokens": int(max_tokens),
                "prompt_version": prompt_version,
            }
        ),
    )


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
    ) -> None:
        if not callable(getattr(provider, "chat", None)):
            raise TypeError("answer provider must expose an async chat(...) method")
        self._provider = provider
        self._temperature = float(temperature)
        self._max_tokens = int(max_tokens)
        self._identity = build_model_identity(
            provider=provider_name,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_version=prompt_version,
        )

    @property
    def identity(self) -> ModelIdentity:
        return self._identity

    async def complete(self, *, system_prompt: str, prompt: str) -> str:
        from src.agent_v2.types import Message, MessageRole, TextBlock

        response = await self._provider.chat(
            [Message(role=MessageRole.USER, blocks=[TextBlock(text=prompt)])],
            tools=None,
            system_prompt=system_prompt,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        return response.text_content()


__all__ = [
    "ANSWER_TEMPERATURE",
    "DEFAULT_MAX_TOKENS",
    "AgentProviderAnswerModel",
    "EvidenceAnswerModel",
    "ModelIdentity",
    "build_model_identity",
]
