"""Loadable fixture provider used by the registry discovery tests.

The fixture never touches credentials or the network: it returns the stable
browser-safe card shape directly, proving that a new provider can be loaded
without changing frontend code.
"""

from __future__ import annotations

from typing import Any, Optional

from dashboard.providers.base import ProviderContext, ProviderSpec, base_card


_PROVIDER_ID = "fixture"
_LABEL = "Fixture Provider"


def fetch(context: ProviderContext) -> Optional[dict[str, Any]]:
    # The fixture intentionally ignores the context: it needs no credentials,
    # no HTTP client, and no failure helpers.
    return base_card(_PROVIDER_ID, _LABEL)


SPEC = ProviderSpec(id=_PROVIDER_ID, label=_LABEL, fetch=fetch)

# A second spec with a built-in ID, used to prove built-in adapters win over
# external duplicates with the same ID.
DEEPSEEK_LOOKALIKE = ProviderSpec(
    id="deepseek",
    label="External DeepSeek Clone",
    fetch=fetch,
)

# Two distinct specs used by the determinism test.
ALPHA_SPEC = ProviderSpec(id="alpha", label="Alpha Provider", fetch=fetch)
ZETA_SPEC = ProviderSpec(id="zeta", label="Zeta Provider", fetch=fetch)

# A non-spec attribute used by the malformed-entry-point tests.
NOT_A_SPEC = "definitely-not-a-provider-spec"
