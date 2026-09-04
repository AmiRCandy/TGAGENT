"""Provider registry.

Adding a provider means writing an adapter and registering a factory here (or,
from outside the package, calling :func:`register_provider`). Nothing else in
the project changes — that is the whole point of the abstraction.

Factories are lazy so that importing this module does not require every optional
SDK to be installed.
"""

from __future__ import annotations

from collections.abc import Callable

from tgagent.config.settings import LLMSettings
from tgagent.errors import LLMConfigError
from tgagent.llm.base import LLMProvider

ProviderFactory = Callable[[LLMSettings], LLMProvider]

_REGISTRY: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
    """Register *factory* under *name*.

    Refuses to clobber an existing name unless ``replace=True``; a silent
    override would be an unpleasant surprise in a plugin ecosystem.
    """
    key = name.strip().lower()
    if not key:
        raise LLMConfigError("Provider name must not be empty.")
    if key in _REGISTRY and not replace:
        raise LLMConfigError(f"Provider {key!r} is already registered.")
    _REGISTRY[key] = factory


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


#: The import name and pip extra for each built-in provider's SDK. Providers
#: registered by other code are absent and assumed to bring their own.
_SDKS: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic", "anthropic"),
    "openai": ("openai", "openai"),
}


def missing_sdk(settings: LLMSettings) -> str | None:
    """The install command the configured provider needs, if it is not importable.

    A provider is constructed on first use, which keeps startup cheap but means
    a deployment with no SDK installed starts cleanly and then fails on the
    first message - the failure mode where the logs say "running" and nothing
    works. Checking the import costs nothing and turns that into one line at
    startup, before anybody has sent anything.
    """
    from importlib.util import find_spec

    entry = _SDKS.get(settings.provider.strip().lower())
    if entry is None:
        return None
    module, extra = entry
    try:
        if find_spec(module) is not None:
            return None
    except (ImportError, ValueError):
        pass
    return f'pip install "tgagent[{extra}]"'


def create_provider(settings: LLMSettings) -> LLMProvider:
    """Instantiate the provider named by ``settings.provider``."""
    key = settings.provider.strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise LLMConfigError(
            f"Unknown LLM provider {settings.provider!r}. "
            f"Available: {', '.join(available_providers()) or '(none)'}."
        )
    return factory(settings)


# ------------------------------------------------------- built-in providers --
def _make_anthropic(settings: LLMSettings) -> LLMProvider:
    from tgagent.llm.providers.anthropic_provider import AnthropicProvider

    return AnthropicProvider(settings)


def _make_openai(settings: LLMSettings) -> LLMProvider:
    from tgagent.llm.providers.openai_provider import OpenAICompatibleProvider

    return OpenAICompatibleProvider(settings)


def _make_fake(settings: LLMSettings) -> LLMProvider:
    from tgagent.llm.providers.fake import FakeProvider

    return FakeProvider(model=settings.model, context_window=settings.context_window)


register_provider("anthropic", _make_anthropic)
register_provider("openai", _make_openai)
# Any OpenAI-compatible gateway is the same adapter with a different base_url;
# the aliases exist so configuration reads honestly.
register_provider("openai-compatible", _make_openai)
register_provider("openrouter", _make_openai)
register_provider("ollama", _make_openai)
register_provider("fake", _make_fake)
