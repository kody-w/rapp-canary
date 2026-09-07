"""ProviderTransport v1: pure protocol conversion, without credentials or I/O."""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass


API_VERSION = 1
CAPABILITIES = frozenset({"catalog", "completion", "streaming", "tool-calls"})


class ProviderError(RuntimeError):
    """A provider configuration or protocol error that must be surfaced."""


@dataclass(frozen=True)
class PluginSpec:
    id: str
    api_version: int
    capabilities: frozenset[str]
    version: str = "1.0.0"


@dataclass(frozen=True)
class PreparedRequest:
    endpoint: str
    body: dict
    stream: bool


@dataclass(frozen=True)
class ProviderStreamEvent:
    chunk: dict | None = None
    completion: dict | None = None

    def __post_init__(self):
        if (self.chunk is None) == (self.completion is None):
            raise ProviderError("A stream event requires either a chunk or a completion")


class ProviderPlugin(ABC):
    spec: PluginSpec

    @abstractmethod
    def supports_model(self, model: dict) -> bool:
        """Claim only models whose advertised protocol this plugin implements."""

    @abstractmethod
    def prepare_request(self, body: dict, model: dict, context: dict) -> PreparedRequest:
        """Translate a Chat Completions request; the host owns authenticated I/O."""

    @abstractmethod
    def translate_response(self, payload: dict, model: dict, context: dict) -> dict:
        """Return a complete Chat Completions object, or raise ProviderError."""

    @abstractmethod
    def translate_stream(
        self, lines: Iterable[str], model: dict, context: dict
    ) -> Iterator[ProviderStreamEvent]:
        """Emit chat chunks and exactly one final completion; reject truncated input."""
