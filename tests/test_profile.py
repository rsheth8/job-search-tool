"""Job-search profile persistence (uses the autouse temp_db fixture)."""
from __future__ import annotations

from app import profile


def test_set_and_get_profile():
    profile.set_profile("u", roles="SWE", locations="Remote, NYC", seniority="new grad")
    row = profile.get_profile("u")
    assert row["roles"] == "SWE"
    assert row["locations"] == "Remote, NYC"
    assert profile.has_profile("u")


def test_partial_update_preserves_other_fields():
    profile.set_profile("u", roles="SWE", locations="NYC")
    profile.set_profile("u", resume_summary="3 yrs backend")  # only this field
    row = profile.get_profile("u")
    assert row["roles"] == "SWE"            # untouched
    assert row["locations"] == "NYC"        # untouched
    assert row["resume_summary"] == "3 yrs backend"


def test_has_profile_false_until_meaningful():
    assert not profile.has_profile("nobody")
    profile.set_profile("nobody", seniority="senior")  # no role/keyword/location
    assert not profile.has_profile("nobody")
    profile.set_profile("nobody", roles="PM")
    assert profile.has_profile("nobody")


def test_profile_text_renders_present_fields_only():
    profile.set_profile("u", roles="SWE", locations="Remote")
    text = profile.profile_text(profile.get_profile("u"))
    assert "Roles: SWE" in text
    assert "Locations: Remote" in text
    assert "Background" not in text  # empty field omitted
