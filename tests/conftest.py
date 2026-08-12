"""Shared fixtures.

Every fixture here is fully offline. There is no code path in the test suite that
can reach Telegram or a model provider, which is the point: CI must not depend on
a personal account or an API key.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeClientManager, FakeTelegramClient, RecordingConfirmation
from tgagent.config.settings import Settings
from tgagent.llm.providers.fake import FakeProvider
from tgagent.security.permissions import PermissionEngine
from tgagent.storage.sqlite import SQLiteStorage
from tgagent.telegram.gateway import CallContext, TelegramGateway
from tgagent.telegram.history import HistoryReader
from tgagent.tools.base import ToolContext


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed entirely at a temporary directory."""
    values = Settings(
        data_dir=tmp_path,
        telegram={"api_id": 12345, "api_hash": "0" * 32, "phone": "+15551234567"},
        llm={"provider": "fake", "model": "fake-model", "stream": False},
        sandbox={"backend": "subprocess", "timeout": 20.0},
        # Tests assert on deterministic behaviour, so throttling is off.
        permissions={"min_seconds_between_writes": 0.0},
    )
    values.ensure_directories()
    return values


@pytest.fixture
async def storage(settings: Settings) -> AsyncIterator[SQLiteStorage]:
    store = SQLiteStorage(settings.storage.database_path)  # type: ignore[arg-type]
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def fake_client() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest.fixture
def manager(fake_client: FakeTelegramClient) -> FakeClientManager:
    return FakeClientManager(fake_client)


@pytest.fixture
def confirmations() -> RecordingConfirmation:
    return RecordingConfirmation(approve=True)


@pytest.fixture
def permissions(settings: Settings) -> PermissionEngine:
    return PermissionEngine(settings.permissions)


@pytest.fixture
def gateway(
    manager: FakeClientManager,
    permissions: PermissionEngine,
    confirmations: RecordingConfirmation,
    settings: Settings,
) -> TelegramGateway:
    return TelegramGateway(
        manager,  # type: ignore[arg-type]
        permissions=permissions,
        confirmations=confirmations,
        audit=None,
        permission_settings=settings.permissions,
        logging_settings=settings.logging,
        features=settings.features,
    )


@pytest.fixture
async def audited_gateway(
    manager: FakeClientManager,
    permissions: PermissionEngine,
    confirmations: RecordingConfirmation,
    settings: Settings,
    storage: SQLiteStorage,
) -> TelegramGateway:
    return TelegramGateway(
        manager,  # type: ignore[arg-type]
        permissions=permissions,
        confirmations=confirmations,
        audit=storage.audit,
        permission_settings=settings.permissions,
        logging_settings=settings.logging,
        features=settings.features,
    )


@pytest.fixture
def history(gateway: TelegramGateway) -> HistoryReader:
    return HistoryReader(gateway)


@pytest.fixture
def call_context() -> CallContext:
    return CallContext(run_id="test-run", origin="tool", interactive=True)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def tool_context(
    settings: Settings, gateway: TelegramGateway, history: HistoryReader
) -> ToolContext:
    return ToolContext(
        run_id="test-run",
        settings=settings,
        conversation_id="conv-1",
        interactive=True,
        gateway=gateway,
        history=history,
        cancelled=asyncio.Event(),
    )


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Strip TGAGENT_* and provider keys so tests see a clean environment."""
    import os

    for key in list(os.environ):
        if key.startswith(("TGAGENT_", "ANTHROPIC_", "OPENAI_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TGAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # so a stray .env in the repo is not picked up
    yield


def make_message(**overrides: Any) -> Any:
    from tests.fakes import FakeMessage

    return FakeMessage(**overrides)
