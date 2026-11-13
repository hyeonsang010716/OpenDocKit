import pytest

from tests import _memo_probe


@pytest.mark.timing
def test_probe_first():
    _memo_probe.probe("first")
