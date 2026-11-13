import sys

import pytest


@pytest.mark.timing
def test_report():
    from tests.conftest import BOUNDARIES
    if sys.platform == "win32":
        pytest.fail("\n".join(BOUNDARIES), pytrace=False)
    print("\n".join(BOUNDARIES))
