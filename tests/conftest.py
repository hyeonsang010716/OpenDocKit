import pytest


# A failed timing test is run up to twice more, a second apart, and passes if one of the runs does: a stall of the shared CI runner
# fails one run, code that became slow fails all three. Only a timing test, and not one marked `no_rerun` (it starts processes
# or threads and checks how they end): elsewhere a second chance would hide a real bug that shows up only sometimes. At most 10
# reruns in a session: when code that became slow fails many timing tests at once, the rest fail on their first run instead of
# tripling a step that would then run into the job's time limit and lose its report. The failure of every attempt that was run
# again is printed, so a rerun that passed still shows what went wrong (Pinned by: test_timing.py::TestTimingTestsAreRunAgain).
TIMING_RERUNS = 2
TIMING_RERUN_DELAY = 1.0
TIMING_MAX_RERUNS = 10


def pytest_configure(config):
    if config.pluginmanager.hasplugin("rerunfailures"):
        if config.option.max_suite_reruns is None:
            config.option.max_suite_reruns = TIMING_MAX_RERUNS
        config.option.rerun_show_tracebacks = True


def pytest_collection_modifyitems(config, items):
    if not config.pluginmanager.hasplugin("rerunfailures"):  # switched off (`-p no:rerunfailures`): every run counts
        return
    for item in items:
        if item.get_closest_marker("timing") is not None and item.get_closest_marker("no_rerun") is None:
            item.add_marker(pytest.mark.flaky(reruns=TIMING_RERUNS, reruns_delay=TIMING_RERUN_DELAY))
