"""Shared Chromium launch policy for the autofill browser tests."""
from __future__ import annotations

import os

import pytest


def skip_unless_ci_chromium(err: BaseException) -> None:
    """Local runs without a browser skip. CI installs Chromium and must fail
    instead of silently reporting green with the autofill suite skipped."""
    if os.environ.get("CI"):
        raise err
    pytest.skip(f"chromium unavailable: {err}")
