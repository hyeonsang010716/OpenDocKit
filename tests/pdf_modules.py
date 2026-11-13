"""The modules the PDF extractor is made of, for tests that replace one of its functions or limits.

A module that uses a name defined in another one holds its own reference to it, so replacing the name in one module leaves
the callers in the others untouched. A test that counts the calls of a function, or lowers a limit, means all of them.
"""

import importlib
import pkgutil

import pytest

from opendockit import extractors


PDF_MODULES = tuple(importlib.import_module(f"{extractors.__name__}.{info.name}")
                    for info in pkgutil.iter_modules(extractors.__path__) if info.name == "pdf" or info.name.startswith("_pdf_"))


def replace_everywhere(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> object:
    """Replace `name` in every PDF module that has it. Returns the original."""
    holders = [module for module in PDF_MODULES if name in vars(module)]
    assert holders, f"no PDF module has {name}"
    original = getattr(holders[0], name)
    for module in holders:
        monkeypatch.setattr(module, name, value)
    return original
