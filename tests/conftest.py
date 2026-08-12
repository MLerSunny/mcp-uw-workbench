from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run anyio tests on asyncio only (no trio dependency)."""
    return "asyncio"
