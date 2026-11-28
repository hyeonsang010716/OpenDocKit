import pytest


# How execnet starts a worker of pytest-xdist: the worker runs this line, and the line evaluates the one it reads from its
# standard input, which `gateway_bootstrap.sendexec` writes. Every reader in execnet 2.1.2 evaluates it so (popen, ssh,
# vagrant and its socket server).
EXECNET_WORKER_LINE = "import sys;exec(eval(sys.stdin.readline()))"


def send_execnet_code_as_ascii() -> bool:
    """Have execnet write the code it sends a new worker as ASCII, so that a worker reads it right in any code page.

    `sendexec` writes the `repr` of the code as UTF-8, and the worker reads it from a pipe in the locale encoding. In code
    page 949, the default of Korean Windows, the path of this checkout's `.venv` came out wrong when it held Hangul (the
    Desktop that OneDrive keeps as `바탕 화면`): the worker ended at once with a UnicodeEncodeError, and `pytest -n auto`
    stopped before a test ran (`EOFError: expected 1 bytes, got 0`; execnet 2.1.2, the newest release). Written with
    `ascii`, the line holds escapes in place of those characters, reads the same in every code page and evaluates to the
    same code. For code that is ASCII, as it is under an ASCII path, `ascii` and `repr` write the same bytes: nothing
    changes there. Left alone, returning False, where execnet is not installed or starts its workers with another line.

    Here, with no import of the suite, because `test_timing.py` runs a copy of this file on its own.
    """
    try:
        from execnet import gateway_bootstrap, gateway_io
    except ImportError:
        return False
    if getattr(gateway_io, "popen_bootstrapline", None) != EXECNET_WORKER_LINE or not hasattr(gateway_bootstrap, "sendexec"):
        return False

    def sendexec(io, *sources: str) -> None:
        io.write((ascii("\n".join(sources)) + "\n").encode("ascii"))

    gateway_bootstrap.sendexec = sendexec
    return True


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
    # Before pytest-xdist starts its workers (`pytest_sessionstart`): their code is sent as ASCII, so that they start
    # under a path that is not ASCII in any code page (Pinned by: test_xdist_workers.py::TestXdistWorkers).
    send_execnet_code_as_ascii()
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
