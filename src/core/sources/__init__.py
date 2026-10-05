"""Search source plugins."""
from .base import REGISTRY, Source, register  # noqa: F401
from . import torrents  # noqa: F401,E402
from . import archive  # noqa: F401,E402
from . import ed2k  # noqa: F401,E402
from . import ed2k_srv  # noqa: F401,E402
from . import kad_source  # noqa: F401,E402


def load_dynamic_sources() -> int:
    """Pick up user-defined eD2k index scrapers from the data directory."""
    n = 0
    for inst in ed2k.refresh_dynamic_sources():
        REGISTRY.add_instance(inst)
        n += 1
    return n


__all__ = ["REGISTRY", "Source", "register", "load_dynamic_sources"]
