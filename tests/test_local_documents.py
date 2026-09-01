"""Transcripts stay on the phone.

Most university-recruiting forms ask for a transcript, and it's the one
attachment the app can't generate. Storing it was a choice between the server
and the device, and the device won -- a transcript carries a school, a student
number, a GPA and every grade someone has ever received.

"Never uploaded" is a promise, and a promise nothing checks is a comment. These
tests check it: the storage layer must not be able to reach the network, and
the screen that drives it must not hold an API client. They're crude -- they
read the Swift as text -- but they fail the moment someone wires an upload in,
which is the only failure that matters here.

The second half is about the feature working at all. A web form's file picker
cannot see inside our container, so a transcript Files can't reach is a
transcript that can never be attached to an application. Two Info.plist keys
are the whole difference, and they live in project.yml because the plist is
generated.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STORE = ROOT / "ios/JobPilot/LocalDocuments.swift"
SCREEN = ROOT / "ios/JobPilot/DocumentsView.swift"
PROJECT = ROOT / "ios/project.yml"

# Anything that could put a file on a wire.
NETWORK = ("URLSession", "APIClient", "dataTask", "upload", "multipart",
           "http://", "https://", "CKRecord", "NSUbiquitous", "iCloud")


@pytest.mark.parametrize("needle", NETWORK)
def test_the_document_store_cannot_reach_the_network(needle):
    source = STORE.read_text()
    # Comments explain the rule; code must not break it.
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("///")
                     and not line.lstrip().startswith("//"))
    assert needle.lower() not in code.lower(), (
        f"LocalDocuments.swift mentions {needle} — device-only was the point")


def test_the_store_imports_nothing_that_could_upload():
    imports = {line.split()[1] for line in STORE.read_text().splitlines()
               if line.startswith("import ")}
    assert imports <= {"Foundation", "UniformTypeIdentifiers"}, imports


def test_the_screen_holds_no_api_client():
    code = SCREEN.read_text()
    assert "APIClient" not in code
    assert "EnvironmentObject var config" not in code


def test_the_folder_is_reachable_from_the_files_app():
    """Without these the transcript is stored somewhere no form can get at."""
    text = PROJECT.read_text()
    assert "UIFileSharingEnabled: true" in text
    assert "LSSupportsOpeningDocumentsInPlace: true" in text


def test_the_screen_tells_the_user_where_the_files_are():
    """A guarantee the user can't see is a guarantee they can't rely on."""
    copy = SCREEN.read_text()
    assert "Never uploaded" in copy
    assert "Files" in copy


def test_nothing_else_writes_into_the_documents_directory():
    """Those plist keys expose the *whole* Documents folder. If another part of
    the app starts caching there, it becomes visible in Files too."""
    others = []
    for path in (ROOT / "ios/JobPilot").glob("*.swift"):
        if path.name in {"LocalDocuments.swift", "DocumentsView.swift"}:
            continue
        if ".documentDirectory" in path.read_text():
            others.append(path.name)
    assert others == [], f"also writes to Documents, now user-visible: {others}"
