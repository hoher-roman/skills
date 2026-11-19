"""One module per technology of getting news. Each module has `DESCRIPTION` and `fetch(source, ctx) -> list[dict]`
and reads its sources from `<module name>.toml` in the config folder.

A raw item is {"url", "title", "description", "published" (aware datetime or None), "categories" (list of str)}.
To add a technology, add a module here and a config file of the same name; nothing else changes.
"""

import importlib
import pkgutil


def technologies():
    """{name: module} of every fetcher module (modules starting with _ are helpers)."""
    out = {}
    for info in pkgutil.iter_modules(__path__):
        if not info.name.startswith("_"):
            out[info.name] = importlib.import_module(f"{__name__}.{info.name}")
    return out
