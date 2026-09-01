"""How the adapters present themselves, and what they claim about it.

JobPilot reads public careers JSON. The bar set for that is: identify as
JobPilot, take a 403 as an answer, and never pretend to be a browser. These
tests hold the code to it, because every one of these is a line someone could
add back in good faith while chasing a failing board.

Also here: two small things that were true but silently wrong. A dict literal
with a repeated key keeps the last one and says nothing, and a company name
titlecased out of a URL slug reads "Janestreet".
"""
from __future__ import annotations

import ast
import collections
import pathlib

import pytest

from app import catalog
from app.jobsources import base, ingest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADAPTERS = sorted((ROOT / "app/jobsources").glob("*.py"))


# --- identifying ourselves -------------------------------------------------

def test_the_user_agent_is_us():
    assert base.USER_AGENT == "JobPilot/1.0"
    assert "Mozilla" not in base.USER_AGENT


@pytest.mark.parametrize("path", ADAPTERS, ids=lambda p: p.name)
def test_no_adapter_claims_to_be_a_browser(path):
    code = path.read_text()
    for marker in ("Mozilla/", "AppleWebKit", "Chrome/", "Safari/",
                   "sec-ch-ua", "Sec-Fetch"):
        assert marker not in code, f"{path.name} sends a browser fingerprint: {marker}"


@pytest.mark.parametrize("path", ADAPTERS, ids=lambda p: p.name)
def test_no_adapter_invents_a_referer(path):
    """A Referer asserts a page visit that never happened. Same pretending as a
    browser UA, one header along."""
    code = "\n".join(line for line in path.read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    assert "Referer" not in code, f"{path.name} sends an invented Referer"


def test_usajobs_is_the_one_documented_exception():
    """It sends the registered email as User-Agent because the API rejects
    anything else. That's a requirement, not a disguise — and the comment
    saying so has to stay, or someone will "fix" it."""
    code = (ROOT / "app/jobsources/usajobs.py").read_text()
    assert '"User-Agent": email' in code
    assert "requires the User-Agent" in code, "the why is missing"


def test_the_pasted_link_path_cannot_reach_the_network():
    """The strongest form of "we never crawl LinkedIn": no HTTP client is in
    scope to do it with."""
    tree = ast.parse((ROOT / "app/jobsources/ingest.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    # urllib.parse is a string operation; urllib.request is a socket.
    network = {"httpx", "requests", "aiohttp", "socket", "urllib.request",
               "http.client", "ftplib", "telnetlib"}
    assert not imported & network, sorted(imported & network)


# --- a repeated key in a dict literal is silent ---------------------------

@pytest.mark.parametrize("path", sorted(
    list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").glob("*.py"))),
    ids=lambda p: str(p.relative_to(ROOT)))
def test_no_dict_literal_repeats_a_key(path):
    """Python keeps the last one and reports nothing. Three of these had crept
    into the catalog builder's sector map, where a wrong survivor would mis-tag
    a company and change which boards get polled."""
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Dict):
            continue
        keys = []
        for k in node.keys:
            if k is None:
                continue
            try:
                keys.append(repr(ast.literal_eval(k)))
            except (ValueError, TypeError, SyntaxError):
                continue
        dupes = [k for k, n in collections.Counter(keys).items() if n > 1]
        assert not dupes, f"{path.name}:{node.lineno} repeats {dupes}"


# --- names ----------------------------------------------------------------

def test_a_known_board_pastes_under_its_real_name():
    assert catalog.display_name("greenhouse", "janestreet") == "Jane Street"
    assert catalog.display_name("greenhouse", "xai") == "xAI"
    posting = ingest.from_url("https://boards.greenhouse.io/janestreet/jobs/7891011")
    assert posting.company == "Jane Street", "titlecased slug leaked through"


def test_an_unknown_board_still_gets_a_readable_name():
    assert catalog.display_name("greenhouse", "nosuchcompany") is None
    posting = ingest.from_url("https://boards.greenhouse.io/nosuchcompany/jobs/1")
    assert posting.company == "Nosuchcompany"


def test_the_name_index_is_dropped_with_the_catalog_it_came_from():
    """It's cached off load(); a reset that misses it serves the old file."""
    catalog.display_name("greenhouse", "janestreet")
    catalog.reset_cache()
    assert catalog._names_by_board.cache_info().currsize == 0
