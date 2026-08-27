"""CI vs local Chromium skip policy — no browser required."""
from __future__ import annotations

import pytest

from tests.browserutil import skip_unless_ci_chromium


def test_skip_unless_ci_chromium_skips_locally(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception, match="chromium unavailable"):
        skip_unless_ci_chromium(RuntimeError("missing binary"))


def test_skip_unless_ci_chromium_fails_the_job_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    with pytest.raises(RuntimeError, match="missing binary"):
        skip_unless_ci_chromium(RuntimeError("missing binary"))
