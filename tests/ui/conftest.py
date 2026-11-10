import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # tests sans écran

import pytest  # noqa: E402

pytest.importorskip("PySide6")
