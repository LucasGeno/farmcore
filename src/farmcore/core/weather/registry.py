"""The installed weather providers, by name.

`register` checks the contract once, up front: every method is there and `licence()` and
`capabilities()` answer with the contract's types. A name is taken once; registering a second
provider under it raises instead of replacing the first.

`installed(manifests, settings)` is the registry of an installation: the providers the given
(active) apps declare in `Manifest.weather_providers`, each built from the settings (its data
directory, endpoint and credentials). A disabled app's provider is not offered; the core imports
no app module except through those declarations.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from farmcore.core.manifest import Manifest, import_string
from farmcore.core.weather.contract import Capabilities, Licence, WeatherProvider
from farmcore.settings import Settings


class ProviderError(ValueError):
    """A provider that does not meet the contract, or a name already taken."""


class Registry:
    def __init__(self) -> None:
        self._providers: dict[str, WeatherProvider] = {}

    def register(self, provider: object) -> WeatherProvider:
        if not isinstance(provider, WeatherProvider):
            raise ProviderError(f"{provider!r} does not implement the weather provider contract")
        name = provider.name
        if not isinstance(name, str) or not name.strip():
            raise ProviderError("a weather provider needs a name")
        if name in self._providers:
            raise ProviderError(f"weather provider {name!r} is already registered")
        if not isinstance(provider.licence(), Licence):
            raise ProviderError(f"{name}: licence() must return a Licence")
        if not isinstance(provider.capabilities(), Capabilities):
            raise ProviderError(f"{name}: capabilities() must return Capabilities")
        self._providers[name] = provider
        return provider

    def get(self, name: str) -> WeatherProvider:
        return self._providers[name]

    def __iter__(self) -> Iterator[WeatherProvider]:
        return iter(self._providers.values())


def installed(manifests: Iterable[Manifest], settings: Settings) -> Registry:
    registry = Registry()
    for manifest in manifests:
        for factory in manifest.weather_providers:
            registry.register(import_string(factory)(settings))
    return registry
