"""P3 unit tests for the answer-model adapter (plan 5.9, decision D-028).

The adapter is deliberately thin: it exposes a model *identity* for the claim
record and one ``complete`` call that reuses the existing Agent provider.  These
tests pin two things that are easy to get wrong and expensive to debug later:

* the recorded ``model_config_hash`` is stable and never contains a secret;
* the provider call is made with deterministic settings and its failures are not
  swallowed -- the service, not the adapter, decides what a failure means.
"""

from __future__ import annotations

import pytest

from src.literature.answer import ANSWER_PROMPT_VERSION
from src.literature.answer_model import (
    DEFAULT_MAX_TOKENS,
    JSON_RESPONSE_FORMAT,
    AgentProviderAnswerModel,
    AnswerModelError,
    EvidenceAnswerModel,
    ModelIdentity,
    build_model_identity,
    effective_thinking_mode,
)

BASE_URL = "https://api.openai.com/v1"


@pytest.fixture
def identity() -> ModelIdentity:
    return build_model_identity(provider="openai", model="gpt-4o", base_url=BASE_URL)


class FakeProvider:
    """Minimal stand-in for the existing Agent provider surface."""

    def __init__(
        self,
        text: str = "{}",
        *,
        api_key: str = "sk-secret-one",
        thinking_mode: str | None = None,
        model: str = "",
        base_url: str = "",
        stop_reason: str = "stop",
    ) -> None:
        self.text = text
        self.api_key = api_key
        self.calls: list[dict] = []
        self.failure: Exception | None = None
        self._stop_reason = stop_reason
        if thinking_mode is not None:
            self.thinking_mode = thinking_mode
        if model:
            self.model = model
        if base_url:
            self.base_url = base_url

    async def chat(
        self,
        messages,
        tools=None,
        system_prompt=None,
        max_tokens=4096,
        temperature=0.3,
        tool_choice="auto",
        response_format=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "system_prompt": system_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": response_format,
            }
        )
        if self.failure is not None:
            raise self.failure
        from src.agent_v2.types import ProviderResponse, TextBlock

        return ProviderResponse(blocks=[TextBlock(text=self.text)], stop_reason=self._stop_reason)


class PlainProvider:
    """Provider whose ``chat`` has no ``response_format`` parameter at all."""

    def __init__(self, text: str = '{"claims":[]}') -> None:
        self.text = text
        self.calls: list[dict] = []

    async def chat(
        self,
        messages,
        tools=None,
        system_prompt=None,
        max_tokens=4096,
        temperature=0.3,
        tool_choice="auto",
    ):
        from src.agent_v2.types import ProviderResponse, TextBlock

        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        return ProviderResponse(blocks=[TextBlock(text=self.text)], stop_reason="stop")


class TestModelIdentity:
    def test_identity_is_frozen_and_hashed(self, identity: ModelIdentity) -> None:
        assert identity.provider == "openai"
        assert identity.model == "gpt-4o"
        assert len(identity.config_hash) == 64
        assert identity.config_hash == identity.config_hash.lower()
        with pytest.raises(ValueError):
            ModelIdentity(provider="", model="gpt-4o", config_hash="a" * 64)

    def test_same_inputs_produce_the_same_hash(self) -> None:
        first = build_model_identity(provider="openai", model="gpt-4o", base_url=BASE_URL)
        second = build_model_identity(provider="openai", model="gpt-4o", base_url=BASE_URL)
        assert first == second

    @pytest.mark.parametrize(
        "override",
        [
            {"provider": "anthropic"},
            {"model": "gpt-4o-mini"},
            {"base_url": "https://api.deepseek.com/v1"},
            {"temperature": 0.5},
            {"max_tokens": 1024},
            {"prompt_version": "evidence_answer_v2"},
        ],
    )
    def test_every_recorded_input_changes_the_hash(self, override: dict) -> None:
        base = {
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": BASE_URL,
            "temperature": 0.0,
            "max_tokens": 2048,
            "prompt_version": ANSWER_PROMPT_VERSION,
        }
        assert (
            build_model_identity(**base).config_hash
            != build_model_identity(**{**base, **override}).config_hash
        )

    def test_prompt_version_defaults_to_the_frozen_answer_prompt(self) -> None:
        assert (
            build_model_identity(provider="openai", model="gpt-4o").config_hash
            == build_model_identity(
                provider="openai", model="gpt-4o", prompt_version=ANSWER_PROMPT_VERSION
            ).config_hash
        )

    def test_config_hash_never_contains_a_secret(self) -> None:
        """A rotated key must not change the recorded configuration hash."""

        first = AgentProviderAnswerModel(
            provider=FakeProvider(api_key="sk-secret-one"),
            provider_name="openai",
            model="gpt-4o",
            base_url=BASE_URL,
        )
        second = AgentProviderAnswerModel(
            provider=FakeProvider(api_key="sk-secret-two"),
            provider_name="openai",
            model="gpt-4o",
            base_url=BASE_URL,
        )
        assert first.identity.config_hash == second.identity.config_hash
        assert "sk-secret" not in first.identity.config_hash


class TestAgentProviderAnswerModel:
    def test_adapter_satisfies_the_narrow_protocol(self) -> None:
        adapter = AgentProviderAnswerModel(
            provider=FakeProvider(), provider_name="openai", model="gpt-4o"
        )
        assert isinstance(adapter, EvidenceAnswerModel)

    def test_identity_reflects_constructor_arguments(self) -> None:
        adapter = AgentProviderAnswerModel(
            provider=FakeProvider(),
            provider_name="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            max_tokens=1024,
        )
        assert adapter.identity.provider == "deepseek"
        assert adapter.identity.model == "deepseek-chat"
        assert (
            adapter.identity.config_hash
            == build_model_identity(
                provider="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                max_tokens=1024,
            ).config_hash
        )

    def test_adapter_rejects_a_provider_without_chat(self) -> None:
        with pytest.raises(TypeError):
            AgentProviderAnswerModel(provider=object(), provider_name="openai", model="gpt-4o")

    async def test_complete_uses_zero_temperature_and_returns_text(self) -> None:
        provider = FakeProvider(text='{"claims":[]}')
        adapter = AgentProviderAnswerModel(
            provider=provider, provider_name="openai", model="gpt-4o", max_tokens=2048
        )
        text = await adapter.complete(system_prompt="system", prompt="prompt")
        assert text == '{"claims":[]}'
        call = provider.calls[0]
        assert call["temperature"] == 0.0
        assert call["max_tokens"] == 2048
        assert call["system_prompt"] == "system"
        assert call["tools"] is None
        assert [block.text for block in call["messages"][0].blocks] == ["prompt"]

    async def test_an_empty_provider_response_is_an_explicit_failure(self) -> None:
        """Thinking models can exhaust max_tokens; that must not look like 'no claims'."""

        provider = FakeProvider(text="")
        adapter = AgentProviderAnswerModel(
            provider=provider, provider_name="openai", model="gpt-4o"
        )

        with pytest.raises(AnswerModelError) as excinfo:
            await adapter.complete(system_prompt="s", prompt="p")

        assert "empty structured response" in str(excinfo.value)

    async def test_complete_propagates_provider_failure(self) -> None:
        provider = FakeProvider()
        provider.failure = RuntimeError("provider exploded")
        adapter = AgentProviderAnswerModel(
            provider=provider, provider_name="openai", model="gpt-4o"
        )
        with pytest.raises(RuntimeError):
            await adapter.complete(system_prompt="s", prompt="p")


class TestJsonOutputMode:
    """DeepSeek's JSON Output guide: ask for json_object and leave token headroom."""

    def test_the_default_budget_leaves_room_for_thinking_plus_the_answer(self) -> None:
        # 2048 truncated real answers (finish_reason=length, empty content).
        assert DEFAULT_MAX_TOKENS >= 8_192

    async def test_json_mode_is_requested_when_the_provider_supports_it(self) -> None:
        provider = FakeProvider(text='{"claims":[]}')
        adapter = AgentProviderAnswerModel(
            provider=provider, provider_name="deepseek", model="deepseek-flash"
        )

        await adapter.complete(system_prompt="s", prompt="p")

        assert provider.calls[0]["response_format"] == JSON_RESPONSE_FORMAT

    async def test_a_provider_without_the_option_is_called_without_it(self) -> None:
        provider = PlainProvider()
        adapter = AgentProviderAnswerModel(
            provider=provider, provider_name="openai", model="gpt-4o"
        )

        assert await adapter.complete(system_prompt="s", prompt="p") == '{"claims":[]}'
        assert provider.calls[0]["max_tokens"] == DEFAULT_MAX_TOKENS

    async def test_the_real_provider_accepts_json_mode(self) -> None:
        from src.agent_v2.providers.openai_compat import OpenAiCompatProvider

        provider = OpenAiCompatProvider(
            api_key="k", model="deepseek-flash", base_url="https://api.deepseek.com"
        )
        adapter = AgentProviderAnswerModel(
            provider=provider, provider_name="deepseek", model="deepseek-flash"
        )

        assert adapter._request_json is True

    async def test_a_truncated_response_names_the_stop_reason(self) -> None:
        provider = FakeProvider(text="", stop_reason="length")
        adapter = AgentProviderAnswerModel(
            provider=provider, provider_name="deepseek", model="deepseek-flash"
        )

        with pytest.raises(AnswerModelError) as excinfo:
            await adapter.complete(system_prompt="s", prompt="p")

        assert "stop_reason=length" in str(excinfo.value)


class TestThinkingModeReporting:
    """The answer path must never override the user's model configuration."""

    def test_a_provider_without_thinking_control_reports_none(self) -> None:
        provider = FakeProvider()

        assert effective_thinking_mode(provider) is None
        assert not hasattr(provider, "thinking_mode")

    def test_an_explicit_enabled_mode_is_reported_and_left_untouched(self) -> None:
        provider = FakeProvider(
            thinking_mode="enabled", model="deepseek-flash", base_url="https://api.deepseek.com"
        )

        assert effective_thinking_mode(provider) == "enabled"
        # Reporting is read-only: the provider keeps the configuration the user set.
        assert provider.thinking_mode == "enabled"

    def test_the_vendor_default_is_reported_for_a_deepseek_v4_model(self) -> None:
        provider = FakeProvider(
            thinking_mode="auto", model="deepseek-flash", base_url="https://api.deepseek.com"
        )

        assert effective_thinking_mode(provider) == "disabled"

    def test_identity_records_thinking_mode_and_whether_temperature_applied(self) -> None:
        thinking = AgentProviderAnswerModel(
            provider=FakeProvider(
                thinking_mode="enabled",
                model="deepseek-flash",
                base_url="https://api.deepseek.com",
            ),
            provider_name="deepseek",
            model="deepseek-flash",
            base_url="https://api.deepseek.com",
        )
        assert thinking.identity.thinking_mode == "enabled"
        assert thinking.identity.temperature_applied is False

        plain = AgentProviderAnswerModel(
            provider=FakeProvider(thinking_mode="disabled"),
            provider_name="openai",
            model="gpt-4o",
        )
        assert plain.identity.thinking_mode == "disabled"
        assert plain.identity.temperature_applied is True

    def test_thinking_mode_changes_the_recorded_config_hash(self) -> None:
        base = build_model_identity(provider="deepseek", model="deepseek-flash")
        thinking = build_model_identity(
            provider="deepseek", model="deepseek-flash", thinking_mode="enabled"
        )

        assert base.config_hash != thinking.config_hash
