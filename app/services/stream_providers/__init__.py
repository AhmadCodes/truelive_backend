"""
Stream providers: vendors that mint HLS addresses for their cameras.

* base.py      the interface (StreamProvider, Address, ProviderError)
* registry.py  the provider types
* lumana.py    the first type
* engine.py    resolution, caching, renewal, delivery and status; no vendor code

Adding a vendor: one module with a StreamProvider subclass, one ``register``
line here, and its tests (docs/STREAM_PROVIDERS.md).
"""
from app.services.stream_providers import registry
from app.services.stream_providers.lumana import LumanaProvider

registry.register(LumanaProvider())
