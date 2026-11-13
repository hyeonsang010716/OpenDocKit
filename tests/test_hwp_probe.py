import pytest

from tests import _memo_probe


@pytest.mark.timing
def test_probe_after_hwp():
    _memo_probe.probe("after hwp")
