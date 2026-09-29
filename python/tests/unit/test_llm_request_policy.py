"""Request-policy tests for DeepSeek's renamed V4.1 model families.

DeepSeek's pricing page documents that the V4.1 Flash model is now called
``deepseek-flash`` and that ``deepseek-v4-flash`` is a deprecated alias served by
the same model.  The automatic non-thinking policy must keep matching the family
under both names, otherwise the tool loop silently runs in thinking mode.
"""

from __future__ import annotations

import pytest

from src.llm_request_policy import (
    apply_reasoning_effort_policy,
    apply_thinking_policy,
    normalize_thinking_mode,
    resolve_thinking_mode,
)

OFFICIAL = "https://api.deepseek.com"


class TestResolveThinkingMode:
    @pytest.mark.parametrize(
        "model",
        ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"],
    )
    def test_official_v4_family_defaults_to_non_thinking(self, model: str) -> None:
        assert resolve_thinking_mode(OFFICIAL, model) == "disabled"

    @pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner"])
    def test_legacy_names_keep_their_provider_default(self, model: str) -> None:
        assert resolve_thinking_mode(OFFICIAL, model) is None

    def test_an_explicit_mode_wins(self) -> None:
        assert resolve_thinking_mode(OFFICIAL, "deepseek-flash", "enabled") == "enabled"
        assert resolve_thinking_mode(OFFICIAL, "deepseek-flash", "disabled") == "disabled"

    def test_other_endpoints_get_no_vendor_parameter(self) -> None:
        assert resolve_thinking_mode("https://api.openai.com/v1", "deepseek-flash") is None
        assert resolve_thinking_mode("https://example.com/v1", "deepseek-v4-pro") is None

    def test_invalid_mode_falls_back_to_auto(self) -> None:
        assert normalize_thinking_mode("sometimes") == "auto"
        assert resolve_thinking_mode(OFFICIAL, "deepseek-flash", "sometimes") == "disabled"


class TestApplyThinkingPolicy:
    def test_payload_carries_the_resolved_flag(self) -> None:
        payload: dict[str, object] = {"temperature": 0.0}

        resolved = apply_thinking_policy(payload, base_url=OFFICIAL, model="deepseek-flash")

        assert resolved == "disabled"
        assert payload["thinking"] == {"type": "disabled"}

    def test_non_deepseek_payload_is_untouched(self) -> None:
        payload: dict[str, object] = {"temperature": 0.0}

        resolved = apply_thinking_policy(
            payload, base_url="https://api.openai.com/v1", model="gpt-4o"
        )

        assert resolved is None
        assert "thinking" not in payload


class TestReasoningEffort:
    def test_effort_is_sent_only_for_official_v4(self) -> None:
        payload: dict[str, object] = {}

        assert (
            apply_reasoning_effort_policy(
                payload, base_url=OFFICIAL, model="deepseek-flash", configured="high"
            )
            == "high"
        )
        assert payload["reasoning_effort"] == "high"

        other: dict[str, object] = {}
        assert (
            apply_reasoning_effort_policy(
                other, base_url="https://api.openai.com/v1", model="gpt-4o", configured="high"
            )
            is None
        )
        assert "reasoning_effort" not in other

    def test_an_unknown_effort_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            apply_reasoning_effort_policy(
                {}, base_url=OFFICIAL, model="deepseek-flash", configured="turbo"
            )
