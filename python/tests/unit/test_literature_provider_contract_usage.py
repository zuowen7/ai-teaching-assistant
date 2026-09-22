"""Guard that every provider implementation runs the shared contract assertions.

``provider_contract_helpers.assert_provider_contract`` is a plain helper module,
so pytest never collects it and nothing would fail if a provider stopped calling
it.  This meta-test pins the plan's "both providers pass the same contract" gate
by checking that each provider test module imports and invokes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEST_DIR = Path(__file__).parent
_IMPORT = "from tests.unit.provider_contract_helpers import assert_provider_contract"
_CALL = "assert_provider_contract("

PROVIDER_TEST_MODULES = (
    "test_literature_provider.py",
    "test_literature_arxiv_provider.py",
)


@pytest.mark.parametrize("module_name", PROVIDER_TEST_MODULES)
def test_provider_test_module_invokes_the_shared_contract(module_name: str) -> None:
    source = (_TEST_DIR / module_name).read_text(encoding="utf-8")

    assert _IMPORT in source, f"{module_name} no longer imports the shared contract helper"
    assert _CALL in source, f"{module_name} no longer invokes the shared contract helper"


def test_shared_contract_helper_is_not_a_collected_test_module() -> None:
    helper = _TEST_DIR / "provider_contract_helpers.py"

    assert helper.is_file()
    assert not helper.name.startswith("test_")
