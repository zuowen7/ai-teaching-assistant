"""Global test configuration.

The product normalizes an unparsable ``NO_PROXY`` list at its entry points
(``src/net_env.py``); the tests import application modules directly, so they do the
same here.  Without it, any test that opens an httpx client fails for a reason that
has nothing to do with the code under test.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.net_env import normalize_proxy_env  # noqa: E402

normalize_proxy_env()
