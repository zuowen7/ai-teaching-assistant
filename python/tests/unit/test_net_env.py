"""Unit tests for the shared network-environment hygiene."""

from __future__ import annotations

import httpx
import pytest

from src.net_env import normalize_proxy_env


class TestNormalizeProxyEnv:
    """httpx parses NO_PROXY while building a client; bad entries must not survive."""

    def test_bracketed_ipv6_entry_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")

        changed = normalize_proxy_env()

        assert changed == {"NO_PROXY": "localhost,127.0.0.1,::1"}
        assert httpx.Client(base_url="http://127.0.0.1:1") is not None

    def test_parsable_entries_are_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,.internal,10.*")

        changed = normalize_proxy_env()

        assert changed == {}
        assert "10.*" in __import__("os").environ["NO_PROXY"]

    def test_lowercase_variant_is_handled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.setenv("no_proxy", "[::1],localhost")

        changed = normalize_proxy_env()

        # On Windows ``os.environ`` is case-insensitive, so the recorded key may be
        # the upper-case spelling of the same variable.
        assert list(changed.values()) == ["localhost"]

    def test_missing_variable_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        assert normalize_proxy_env() == {}

    def test_blank_entries_are_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_PROXY", " localhost , ,127.0.0.1 ")

        changed = normalize_proxy_env()

        assert changed == {"NO_PROXY": "localhost,127.0.0.1"}

    def test_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_PROXY", "[::1],localhost")

        first = normalize_proxy_env()
        second = normalize_proxy_env()

        assert first == {"NO_PROXY": "localhost"}
        assert second == {}
