import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_root_logging():
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if not h.__class__.__module__.startswith(("_pytest", "pytest")):
            root.removeHandler(h)
