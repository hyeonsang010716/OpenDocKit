"""Importing this module imports every extractor at once. For a process that forks workers and wants them to start with all
of them loaded (the forkserver of `IsolatedConverter`); everything else imports an extractor on first use
(`opendockit.extractors.EXTRACTORS`).

They are imported through `load`, under the lock every import on first use shares: imported around it, a thread importing this
module while others converted their first documents imported the same libraries at once, the `_DeadlockError` that lock is
there for (Pinned by: test_converter.py::TestExtractorsImportedOnFirstUse::test_importing_them_all_goes_through_the_lock).
"""

from opendockit.extractors import EXTRACTOR_MODULES, load


for _module in sorted({module for module, _ in EXTRACTOR_MODULES.values()}):
    load(f"opendockit.extractors.{_module}")
