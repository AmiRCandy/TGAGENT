"""Serialisation, the schema index, entity coercion, media, login, and paging."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes import (
    FakeDialog,
    FakeDocument,
    FakeEntity,
    FakeMedia,
    FakeMessage,
    FakeTelegramClient,
)
from tgagent.config.settings import MediaSettings, TelegramSettings
from tgagent.errors import (
    AuthenticationError,
    EntityResolutionError,
    MediaTooLarge,
    MediaTypeRejected,
)
from tgagent.telegram.auth import LoginFlow
from tgagent.telegram.client import parse_proxy
from tgagent.telegram.entities import (
    EntityResolver,
    coerce_argument,
    extract_target,
    parse_datetime,
)
from tgagent.telegram.history import HistoryReader
from tgagent.telegram.media import MediaManager, sanitise_filename
from tgagent.telegram.schema import TelegramSchemaIndex, format_entry
from tgagent.telegram.serialize import (
    dialog_to_dict,
    entity_to_dict,
    extract_text_fields,
    message_to_dict,
    to_jsonable,
    truncate,
)


class MessageMediaPhoto:
    """A photo attachment, named after Telethon's class.

    The media summary reports ``type`` from the class name, and the download
    policy has to key off it: a photo carries neither a MIME type nor a size,
    unlike a document.
    """

    def __init__(self) -> None:
        self.document = None
        self.photo = SimpleNamespace(id=77, sizes=[])
        self.webpage = None
        self.poll = None
        self.geo = None
        self.phone_number = None


class TestSerialisation:
    def test_message_projection_is_compact_and_complete(self) -> None:
        message = FakeMessage(42, "hello there", sender_id=7)
        payload = message_to_dict(message)
        assert payload["id"] == 42
        assert payload["text"] == "hello there"
        assert payload["sender_id"] == 7
        assert "date" in payload
        # Nulls are omitted rather than serialised, which is most of the saving.
        assert "reply_to_msg_id" not in payload

    def test_media_metadata_only_never_contents(self) -> None:
        message = FakeMessage(1, "see attached", media=FakeMedia(FakeDocument()))
        payload = message_to_dict(message)
        assert payload["media"]["mime_type"] == "application/pdf"
        assert payload["media"]["file_name"] == "report.pdf"
        assert "content" not in payload["media"]
        assert "bytes" not in str(payload["media"])

    def test_long_text_is_truncated_with_a_marker(self) -> None:
        payload = message_to_dict(FakeMessage(1, "x" * 20_000), max_text=500)
        assert len(payload["text"]) < 700
        assert "truncated" in payload["text"]

    def test_dialog_projection(self) -> None:
        payload = dialog_to_dict(FakeDialog(1, "@alex", unread=4, message=FakeMessage(9, "hi")))
        assert payload["unread_count"] == 4
        assert payload["last_message"]["text"] == "hi"

    def test_zero_valued_fields_are_still_emitted(self) -> None:
        # A channel with no members must be distinguishable from one whose member
        # count Telegram did not report.
        entity = FakeEntity(1, title="Nobody here", broadcast=True)
        entity.participants_count = 0  # type: ignore[attr-defined]
        payload = entity_to_dict(entity)
        assert payload["participants_count"] == 0

    def test_phone_numbers_are_partially_masked(self) -> None:
        entity = FakeEntity(1, first_name="Alex")
        entity.phone = "15551234567"
        payload = entity_to_dict(entity)
        assert payload["phone"] == "…4567"
        assert "15551234567" not in str(payload)

    def test_bytes_are_never_emitted_raw(self) -> None:
        payload = to_jsonable({"blob": b"\x00\x01" * 500})
        assert payload["blob"]["__bytes__"] == "<omitted>"
        assert payload["blob"]["length"] == 1000

    def test_small_bytes_are_base64_encoded(self) -> None:
        payload = to_jsonable({"blob": b"hello"})
        assert payload["blob"]["__bytes__"]
        assert payload["blob"]["length"] == 5

    def test_circular_references_are_survivable(self) -> None:
        a: dict = {"name": "a"}
        b = {"name": "b", "parent": a}
        a["child"] = b
        payload = to_jsonable(a)
        assert "circular reference" in str(payload)

    def test_depth_is_bounded(self) -> None:
        node: dict = {"leaf": True}
        for _ in range(50):
            node = {"child": node}
        assert "depth limit" in str(to_jsonable(node, max_depth=5))

    def test_lists_are_capped(self) -> None:
        payload = to_jsonable(list(range(5_000)), max_items=10)
        assert len(payload) == 11
        assert "more items" in str(payload[-1])

    def test_forbidden_attributes_are_stripped(self) -> None:
        class Sensitive:
            def __init__(self) -> None:
                self.auth_key = b"super secret"
                self.api_hash = "0123456789abcdef0123456789abcdef"
                self.harmless = "fine"

        payload = to_jsonable(Sensitive())
        assert payload["harmless"] == "fine"
        assert "auth_key" not in payload
        assert "api_hash" not in payload

    def test_datetimes_become_iso_strings(self) -> None:
        payload = to_jsonable({"when": datetime(2026, 1, 1, tzinfo=UTC)})
        assert payload["when"].startswith("2026-01-01")

    def test_extract_text_fields_finds_every_writable_surface(self) -> None:
        payload = {
            "messages": [{"text": "body"}, {"text": "another"}],
            "media": {"file_name": "invoice.pdf"},
            "chats": [{"title": "Project X"}],
            "users": [{"username": "alex", "first_name": "Alex"}],
            "ignored": {"count": 5},
        }
        found = extract_text_fields(payload)
        assert {"body", "another", "invoice.pdf", "Project X", "alex", "Alex"} <= set(found)

    def test_truncate(self) -> None:
        assert truncate("short", 100) == "short"
        assert "truncated" in truncate("x" * 200, 50)


class TestSchemaIndex:
    @pytest.fixture
    def index(self, tmp_path: Path) -> TelegramSchemaIndex:
        return TelegramSchemaIndex(tmp_path / "schema.json")

    def test_covers_the_generated_api_surface(self, index: TelegramSchemaIndex) -> None:
        # Reflecting over the installed package is what makes the index accurate.
        assert len(index) > 800

    def test_exact_lookup(self, index: TelegramSchemaIndex) -> None:
        entry = index.get("messages.Search")
        assert entry is not None
        assert entry.kind == "tl_request"
        assert {p["name"] for p in entry.parameters} >= {"peer", "q", "limit"}

    def test_lookup_tolerates_the_request_suffix(self, index: TelegramSchemaIndex) -> None:
        assert index.get("messages.SearchRequest") is not None
        assert index.get("MESSAGES.SEARCH") is not None

    def test_search_ranks_exact_matches_first(self, index: TelegramSchemaIndex) -> None:
        hits = index.search("messages.Search", limit=5)
        assert hits[0].entry.path == "messages.Search"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("send a message", "send"),
            ("delete history", "delete"),
            ("download", "download"),
            ("participants of a channel", "articipant"),
        ],
    )
    def test_intent_search_finds_relevant_methods(
        self, index: TelegramSchemaIndex, query: str, expected: str
    ) -> None:
        hits = index.search(query, limit=6)
        assert any(expected.lower() in h.entry.path.lower() for h in hits), [
            h.entry.path for h in hits
        ]

    def test_kind_filter(self, index: TelegramSchemaIndex) -> None:
        hits = index.search("message", limit=10, kind="client_method")
        assert hits and all(h.entry.kind == "client_method" for h in hits)

    def test_empty_query_returns_nothing(self, index: TelegramSchemaIndex) -> None:
        assert index.search("") == []

    def test_cache_is_written_and_reused(self, tmp_path: Path) -> None:
        cache = tmp_path / "schema.json"
        first = TelegramSchemaIndex(cache)
        first.ensure_loaded()
        assert cache.exists()

        second = TelegramSchemaIndex(cache)
        second.ensure_loaded()
        assert len(second) == len(first)

    def test_corrupt_cache_is_rebuilt(self, tmp_path: Path) -> None:
        cache = tmp_path / "schema.json"
        cache.write_text("{ not json", encoding="utf-8")
        index = TelegramSchemaIndex(cache)
        index.ensure_loaded()
        assert len(index) > 100

    @pytest.mark.parametrize("body", ["[]", "null", "3"])
    def test_a_cache_that_is_not_an_object_is_rebuilt(self, tmp_path: Path, body: str) -> None:
        # Valid JSON of the wrong shape is as unusable as invalid JSON.
        cache = tmp_path / "schema.json"
        cache.write_text(body, encoding="utf-8")
        index = TelegramSchemaIndex(cache)
        index.ensure_loaded()
        assert len(index) > 100

    def test_format_entry_shows_how_to_call_it(self, index: TelegramSchemaIndex) -> None:
        rendered = format_entry(index.get("messages.Search"))
        assert 'tg.invoke_raw("messages.Search"' in rendered
        assert format_entry(index.get("send_message")).count("tg.send_message(") == 1


class TestEntityCoercion:
    async def test_string_peer_becomes_an_input_entity(
        self, fake_client: FakeTelegramClient
    ) -> None:
        resolver = EntityResolver(fake_client)
        coerced = await coerce_argument("@alex", "TypeInputPeer", resolver)
        assert coerced.id == "@alex"

    async def test_resolution_is_cached(self, fake_client: FakeTelegramClient) -> None:
        resolver = EntityResolver(fake_client)
        await resolver.input_entity("@alex")
        await resolver.input_entity("@alex")
        assert sum(1 for name, _ in fake_client.calls if name == "get_input_entity") == 1

    async def test_unresolvable_peer_gives_actionable_advice(
        self, fake_client: FakeTelegramClient
    ) -> None:
        resolver = EntityResolver(fake_client)
        with pytest.raises(EntityResolutionError, match="list your dialogs"):
            await resolver.input_entity("@missing")

    async def test_iso_string_becomes_a_datetime(self, fake_client: FakeTelegramClient) -> None:
        resolver = EntityResolver(fake_client)
        coerced = await coerce_argument("2026-01-31T12:00:00Z", "datetime", resolver)
        assert coerced.year == 2026 and coerced.tzinfo is not None

    async def test_tagged_dict_constructs_a_tl_type(self, fake_client: FakeTelegramClient) -> None:
        resolver = EntityResolver(fake_client)
        coerced = await coerce_argument(
            {"_": "InputMessagesFilterPhotos"}, "TypeMessagesFilter", resolver
        )
        assert type(coerced).__name__ == "InputMessagesFilterPhotos"

    async def test_unknown_tl_type_is_reported(self, fake_client: FakeTelegramClient) -> None:
        resolver = EntityResolver(fake_client)
        with pytest.raises(EntityResolutionError, match="telegram_api_search"):
            await coerce_argument({"_": "NotARealType"}, "Type", resolver)

    def test_parse_datetime_variants(self) -> None:
        assert parse_datetime("2026-01-31").year == 2026
        assert parse_datetime("2026-01-31T10:00:00Z").tzinfo is not None
        with pytest.raises(EntityResolutionError, match="ISO-8601"):
            parse_datetime("last tuesday")

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ({"peer": "@alex"}, "@alex"),
            ({"entity": -100123}, "-100123"),
            ({"to_peer": "@bob", "from_peer": "@a"}, "@bob"),
            ({"limit": 5}, None),
        ],
    )
    def test_extract_target(self, arguments: dict, expected: str | None) -> None:
        assert extract_target(arguments) == expected


class TestProxyParsing:
    def test_socks5_with_credentials(self) -> None:
        parsed = parse_proxy("socks5://user:pass@127.0.0.1:1080")
        assert parsed[1] == "127.0.0.1" and parsed[2] == 1080 and parsed[4] == "user"

    def test_http_without_credentials(self) -> None:
        parsed = parse_proxy("http://127.0.0.1:8080")
        assert len(parsed) == 3

    def test_empty_is_none(self) -> None:
        assert parse_proxy("") is None

    def test_unsupported_scheme_rejected(self) -> None:
        from tgagent.errors import ConfigError

        with pytest.raises(ConfigError, match="Unsupported proxy scheme"):
            parse_proxy("ftp://host:21")

    def test_missing_port_rejected(self) -> None:
        from tgagent.errors import ConfigError

        with pytest.raises(ConfigError, match="host and port"):
            parse_proxy("socks5://host")


class TestFilenameSanitisation:
    @pytest.mark.parametrize(
        ("raw", "forbidden"),
        [
            ("../../etc/passwd", ".."),
            ("..\\..\\windows\\system32\\cmd.exe", ".."),
            ("/absolute/path.txt", "/"),
            ("C:\\Windows\\evil.dll", "\\"),
            ("nested/dir/file.pdf", "/"),
        ],
    )
    def test_path_traversal_is_stripped(self, raw: str, forbidden: str) -> None:
        cleaned = sanitise_filename(raw)
        assert forbidden not in cleaned
        assert Path(cleaned).name == cleaned

    def test_control_characters_removed(self) -> None:
        assert "\x00" not in sanitise_filename("bad\x00name.txt")

    def test_windows_reserved_names_are_escaped(self) -> None:
        assert sanitise_filename("CON.txt") != "CON.txt"
        assert sanitise_filename("LPT1") != "LPT1"

    def test_long_names_are_shortened_but_keep_the_extension(self) -> None:
        cleaned = sanitise_filename("a" * 500 + ".pdf")
        assert len(cleaned) <= 120
        assert cleaned.endswith(".pdf")

    def test_empty_falls_back(self) -> None:
        assert sanitise_filename("") == "file"
        assert sanitise_filename("...") == "file"
        assert sanitise_filename(None) == "file"

    def test_ordinary_names_survive(self) -> None:
        assert sanitise_filename("Q4 report (final).pdf").endswith(".pdf")

    @pytest.mark.parametrize(
        "fallback", ["https://t.me/chan_5", "../../x_5", "/etc/shadow", "..", "..\\..\\x"]
    )
    def test_the_fallback_is_sanitised_too(self, fallback: str) -> None:
        # The fallback is built from a caller-supplied peer, so it is no more
        # trustworthy than the name it stands in for.
        cleaned = sanitise_filename(None, fallback=fallback)
        assert Path(cleaned).name == cleaned
        assert "/" not in cleaned and "\\" not in cleaned
        assert ".." not in cleaned


class TestMediaValidation:
    @pytest.fixture
    def media(self, gateway: object, tmp_path: Path) -> MediaManager:
        return MediaManager(
            gateway,  # type: ignore[arg-type]
            MediaSettings(download_dir=tmp_path, max_file_bytes=2_048),
            root=tmp_path,
        )

    def test_oversized_files_are_refused_before_transfer(self, media: MediaManager) -> None:
        with pytest.raises(MediaTooLarge, match="over the"):
            media.check_metadata(size=5_000, mime_type="application/pdf", file_name="big.pdf")

    def test_blocked_extensions_are_refused(self, media: MediaManager) -> None:
        for name in ("payload.exe", "script.ps1", "installer.msi", "thing.bat"):
            with pytest.raises(MediaTypeRejected, match="extension is blocked"):
                media.check_metadata(size=10, mime_type="application/pdf", file_name=name)

    def test_disallowed_mime_types_are_refused(self, media: MediaManager) -> None:
        with pytest.raises(MediaTypeRejected, match="not on the allow-list"):
            media.check_metadata(
                size=10, mime_type="application/x-msdownload", file_name="thing.bin"
            )

    def test_permitted_file_passes(self, media: MediaManager) -> None:
        media.check_metadata(size=500, mime_type="application/pdf", file_name="ok.pdf")

    async def test_download_writes_inside_the_run_directory(
        self, media: MediaManager, fake_client: FakeTelegramClient, tmp_path: Path
    ) -> None:
        fake_client.messages = [FakeMessage(5, "here", media=FakeMedia(FakeDocument(size=100)))]
        result = await media.download_message_media("@alex", 5, run_id="run-1")
        assert result.path.exists()
        assert result.path.parent == tmp_path / "run-1"
        assert result.size_bytes > 0

    async def test_download_refuses_a_blocked_type(
        self, media: MediaManager, fake_client: FakeTelegramClient
    ) -> None:
        fake_client.messages = [
            FakeMessage(
                6,
                "run me",
                media=FakeMedia(
                    FakeDocument(size=100, mime_type="application/pdf", file_name="virus.exe")
                ),
            )
        ]
        with pytest.raises(MediaTypeRejected):
            await media.download_message_media("@alex", 6, run_id="run-1")

    def test_documents_without_a_mime_type_are_refused(self, media: MediaManager) -> None:
        # Nothing identifies the file, so the allow-list cannot clear it.
        for mime in ("", None, "   "):
            with pytest.raises(MediaTypeRejected, match="MIME"):
                media.check_metadata(size=10, mime_type=mime, file_name="payload.bin")

    def test_photos_are_allowed_despite_carrying_no_mime_type(self, media: MediaManager) -> None:
        media.check_metadata(
            size=None, mime_type=None, file_name="chan_5", media_type="MessageMediaPhoto"
        )

    def test_photos_are_refused_when_images_are_off_the_allow_list(
        self, gateway: object, tmp_path: Path
    ) -> None:
        manager = MediaManager(
            gateway,  # type: ignore[arg-type]
            MediaSettings(download_dir=tmp_path, allowed_mime_prefixes=["application/pdf"]),
            root=tmp_path,
        )
        with pytest.raises(MediaTypeRejected, match="allow-list"):
            manager.check_metadata(
                size=None, mime_type=None, file_name="chan_5", media_type="MessageMediaPhoto"
            )

    async def test_a_photo_download_succeeds(
        self, media: MediaManager, fake_client: FakeTelegramClient, tmp_path: Path
    ) -> None:
        fake_client.messages = [FakeMessage(8, "look", media=MessageMediaPhoto())]
        result = await media.download_message_media("@alex", 8, run_id="run-1")
        assert result.path.parent == tmp_path / "run-1"

    async def test_a_traversing_peer_cannot_place_a_download_outside_the_run_directory(
        self, media: MediaManager, fake_client: FakeTelegramClient, tmp_path: Path
    ) -> None:
        # Photos carry no filename, so the peer-derived fallback names the file.
        fake_client.messages = [FakeMessage(9, "look", media=MessageMediaPhoto())]
        result = await media.download_message_media("../../escape", 9, run_id="run-1")
        assert result.path.parent == tmp_path / "run-1"
        assert not (tmp_path.parent / "escape_9").exists()

    def test_cleanup_leaves_a_directory_a_live_run_is_using(
        self, media: MediaManager, tmp_path: Path
    ) -> None:
        # Reaping a run directory between its creation and the download makes
        # Telethon write into a missing parent.
        directory = media.run_directory("live-run")
        media.cleanup()
        assert directory.exists()

    def test_cleanup_removes_only_old_files(self, media: MediaManager, tmp_path: Path) -> None:
        import os
        import time

        old = tmp_path / "run-old" / "stale.pdf"
        old.parent.mkdir(parents=True)
        old.write_text("x")
        os.utime(old, (time.time() - 86400 * 30, time.time() - 86400 * 30))

        fresh = tmp_path / "run-new" / "fresh.pdf"
        fresh.parent.mkdir(parents=True)
        fresh.write_text("y")

        assert media.cleanup() == 1
        assert not old.exists()
        assert fresh.exists()


class _UnauthorisedClient:
    """A client that connects but has no session, so login has to prompt."""

    def __init__(self) -> None:
        self.phones: list[str] = []

    def is_connected(self) -> bool:
        return True

    async def connect(self) -> None:
        return None

    async def is_user_authorized(self) -> bool:
        return False

    async def send_code_request(self, phone: str) -> SimpleNamespace:
        self.phones.append(phone)
        return SimpleNamespace(phone_code_hash="hash", type=SimpleNamespace())


class _LoginManager:
    """Enough of ``TelegramClientManager`` for the login flow to run offline."""

    def __init__(self, settings: TelegramSettings) -> None:
        self._settings = settings
        self._session_path = "/tmp/login-test.session"
        self._client = _UnauthorisedClient()

    def build(self) -> _UnauthorisedClient:
        return self._client

    @property
    def client(self) -> _UnauthorisedClient:
        return self._client


class TestLoginTimeout:
    @staticmethod
    def _settings(timeout: float) -> TelegramSettings:
        return TelegramSettings(api_id=1, api_hash="0" * 32, login_timeout=timeout)

    async def test_an_unanswered_prompt_fails_instead_of_hanging(self) -> None:
        async def never_answers() -> str:
            await asyncio.Event().wait()
            return "unreachable"

        manager = _LoginManager(self._settings(0.05))
        flow = LoginFlow(
            manager,  # type: ignore[arg-type]
            phone=None,
            request_phone=never_answers,
        )
        with pytest.raises(AuthenticationError, match=r"within 0\.05s"):
            await asyncio.wait_for(flow.run(), 5)

    async def test_a_prompt_answered_in_time_is_used(self) -> None:
        async def answers() -> str:
            return "+15551234567"

        manager = _LoginManager(self._settings(30.0))
        flow = LoginFlow(
            manager,  # type: ignore[arg-type]
            phone=None,
            request_phone=answers,
        )
        # No code prompt is supplied, so the flow gets past the phone step and
        # then reports the missing one.
        with pytest.raises(AuthenticationError, match="login code is required"):
            await flow.run()
        assert manager.client.phones == ["+15551234567"]


class _SearchSlice:
    """Shaped like ``messages.MessagesSlice`` for a global search."""

    def __init__(self, messages: list[FakeMessage], *, next_rate: int | None = 4242) -> None:
        self.messages = messages
        self.count = 500
        self.next_rate = next_rate
        self.chats: list[object] = []
        self.users: list[object] = []


class _RecordingGateway:
    """Captures the arguments a reader sends and replies with a canned slice."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def call(
        self,
        method: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: object = None,
        projector: Callable[[Any], Any] | None = None,
    ) -> SimpleNamespace:
        self.calls.append(dict(arguments or {}))
        payload = projector(self.response) if projector is not None else self.response
        return SimpleNamespace(payload=payload)


class TestGlobalSearchPagination:
    @staticmethod
    def _slice(**kwargs: Any) -> _SearchSlice:
        # Global search spans chats, so ids are not comparable across the page.
        return _SearchSlice(
            [
                FakeMessage(90, "first hit", chat_id=-100111),
                FakeMessage(4, "second hit", chat_id=-100222),
            ],
            **kwargs,
        )

    async def test_the_cursor_points_past_the_last_row_of_the_slice(self) -> None:
        gateway = _RecordingGateway(self._slice())
        page = await HistoryReader(gateway).search_global("x", limit=2)  # type: ignore[arg-type]

        assert page.has_more
        payload = page.to_dict()
        assert payload["next_offset_id"] == 4
        assert payload["next_offset_peer"] == -100222
        assert payload["next_offset_rate"] == 4242

    async def test_continuing_a_search_sends_the_whole_three_part_cursor(self) -> None:
        gateway = _RecordingGateway(self._slice())
        reader = HistoryReader(gateway)  # type: ignore[arg-type]
        first = await reader.search_global("x", limit=2)

        await reader.search_global(
            "x",
            limit=2,
            offset_id=first.next_offset_id or 0,
            offset_rate=first.next_offset_rate or 0,
            offset_peer=first.next_offset_peer,
        )
        assert gateway.calls[1]["offset_id"] == 4
        assert gateway.calls[1]["offset_rate"] == 4242
        assert gateway.calls[1]["offset_peer"] == -100222

    async def test_a_caller_threading_only_the_offset_id_still_advances(self) -> None:
        # The tool layer hands the model a single id; the rest of the cursor has
        # to be recovered or the next page repeats the first one.
        gateway = _RecordingGateway(self._slice())
        reader = HistoryReader(gateway)  # type: ignore[arg-type]
        first = await reader.search_global("x", limit=2)

        await reader.search_global("x", limit=2, offset_id=first.next_offset_id or 0)
        assert gateway.calls[1]["offset_rate"] == 4242
        assert gateway.calls[1]["offset_peer"] == -100222

    async def test_no_cursor_is_advertised_when_the_peer_is_unknown(self) -> None:
        # Without a peer the cursor cannot advance, so promising more would make
        # a paginating caller loop over the same page forever.
        slice_without_peers = _SearchSlice(
            [FakeMessage(90, "hit", chat_id=None), FakeMessage(4, "hit", chat_id=None)]  # type: ignore[arg-type]
        )
        gateway = _RecordingGateway(slice_without_peers)
        page = await HistoryReader(gateway).search_global("x", limit=2)  # type: ignore[arg-type]

        assert not page.has_more
        assert page.next_offset_id is None
        assert "next_offset_id" not in page.to_dict()
