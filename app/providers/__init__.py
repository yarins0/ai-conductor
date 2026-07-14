"""Providers package.

`base.py` holds the Provider contract + Tool Registry; one module per tool
(`reach.py` / `qualify.py` / `book.py`) holds that tool's simulated provider (the
credential-free default) and its real, credential-gated adapter. Importing this
package registers all of them as a side effect, so consumers only need
`import app.providers` (or `from app.providers import get_provider`, etc.).

STEP_DELAY_SECONDS lives here rather than in a submodule so all three simulated
providers share one knob and the tests can monkeypatch `app.providers.STEP_DELAY_SECONDS`.
The simulated providers read it off the package at call time, so the patch is honored.
"""

STEP_DELAY_SECONDS = 0.5  # tests monkeypatch this to 0; sim providers read it at call time

# Re-export the contract + registry so consumers import from the package, not base.
from app.providers.base import (  # noqa: E402, F401
    CredentialGatedProvider,
    DEFAULT_PROVIDER,
    PROVIDER_LABELS,
    Provider,
    ProviderConfigError,
    TOOL_REGISTRY,
    ToolResult,
    get_provider,
    list_providers,
    register_provider,
)

# Import order defines dropdown order; each import registers that tool's providers.
from app.providers import reach, qualify, book  # noqa: E402, F401  (side effect: registration)
