"""Telegram integration: client lifecycle, the gateway, and safe projections."""

from tgagent.telegram.auth import LoginFlow, LoginResult
from tgagent.telegram.client import TelegramClientManager, parse_proxy
from tgagent.telegram.entities import EntityResolver, ResolvedPeer, extract_target
from tgagent.telegram.gateway import CallContext, GatewayResult, TelegramGateway
from tgagent.telegram.history import HistoryPage, HistoryReader
from tgagent.telegram.media import DownloadResult, MediaManager, sanitise_filename
from tgagent.telegram.schema import ApiEntry, TelegramSchemaIndex, format_entry
from tgagent.telegram.serialize import (
    dialog_to_dict,
    entity_to_dict,
    message_to_dict,
    to_jsonable,
)

__all__ = [
    "ApiEntry",
    "CallContext",
    "DownloadResult",
    "EntityResolver",
    "GatewayResult",
    "HistoryPage",
    "HistoryReader",
    "LoginFlow",
    "LoginResult",
    "MediaManager",
    "ResolvedPeer",
    "TelegramClientManager",
    "TelegramGateway",
    "TelegramSchemaIndex",
    "dialog_to_dict",
    "entity_to_dict",
    "extract_target",
    "format_entry",
    "message_to_dict",
    "parse_proxy",
    "sanitise_filename",
    "to_jsonable",
]
