"""Importing this module imports every extractor at once. For a process that forks workers and wants them to start with all
of them loaded (the forkserver of `IsolatedConverter`); everything else imports an extractor on first use
(`opendockit.extractors.EXTRACTORS`)."""

from importlib import import_module

from opendockit.extractors import EXTRACTOR_MODULES


for _module in sorted({module for module, _ in EXTRACTOR_MODULES.values()}):
    import_module(f"opendockit.extractors.{_module}")
