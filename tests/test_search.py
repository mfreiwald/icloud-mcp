"""email_search: multi-word queries, multi-folder search and the local fallback."""

from email.mime.text import MIMEText

import pytest
from test_server import FakeIMAP, _call

from icloud_mcp import mail as mail_module


def _raw(subject, sender, date):
    msg = MIMEText("body", "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "tester@icloud.com"
    msg["Date"] = date
    return (b"\\Seen",), msg.as_bytes()


class FolderIMAP(FakeIMAP):
    """Evaluates the top-level OR(SUBJECT, FROM) clusters email_search sends, per folder."""

    def __init__(self, folders, reject_search=False):
        super().__init__({})
        self.folders = folders
        self.reject_search = reject_search
        self.searches = []

    def select_folder(self, folder, readonly=False):
        self.selected = folder
        self.messages = self.folders[folder]

    def search(self, criteria, charset=None):
        self.searches.append(criteria)
        if criteria == ["ALL"]:
            return list(self.messages)
        if self.reject_search:
            raise RuntimeError("SEARCH not supported")
        words = [c[1][1].lower() for c in criteria if isinstance(c, list) and c[0] == "OR" and c[1][0] == "SUBJECT"]
        hits = []
        for uid, (_, raw) in self.messages.items():
            text = raw.decode().lower()
            if all(w in text for w in words):
                hits.append(uid)
        return hits


@pytest.fixture
def imap(monkeypatch):
    fake = FolderIMAP({
        "INBOX": {
            1: _raw("DHL Tracking 123", "dhl@example.com", "Mon, 01 Sep 2025 10:00:00 +0000"),
            2: _raw("DHL Rechnung", "dhl@example.com", "Tue, 02 Sep 2025 10:00:00 +0000"),
            3: _raw("Tracking update", "ups@example.com", "Wed, 03 Sep 2025 10:00:00 +0000"),
        },
        "Archive": {
            7: _raw("Old DHL Tracking", "dhl@example.com", "Fri, 05 Sep 2025 10:00:00 +0000"),
        },
    })
    monkeypatch.setattr(mail_module, "get_imap_client", lambda u, p: fake)
    monkeypatch.setattr(mail_module, "close_imap_client", lambda c: None)
    return fake


def _subjects(result):
    return [m["subject"] for m in result.structured_content["result"]]


def test_multi_word_query_requires_every_word(imap):
    result = _call("email_search", query="tracking DHL", include_body=False)
    assert _subjects(result) == ["DHL Tracking 123"]
    # one flat OR(SUBJECT, FROM) search per word, no nested OR groups
    assert [s[-1] for s in imap.searches] == [
        ["OR", ["SUBJECT", "tracking"], ["FROM", "tracking"]],
        ["OR", ["SUBJECT", "DHL"], ["FROM", "DHL"]],
    ]


def test_multi_folder_merges_newest_first_with_folder(imap):
    result = _call("email_search", query="DHL tracking", folders=["INBOX", "Archive"], include_body=False)
    items = result.structured_content["result"]
    assert [(m["subject"], m["folder"]) for m in items] == [
        ("Old DHL Tracking", "Archive"),
        ("DHL Tracking 123", "INBOX"),
    ]


def test_multi_folder_limit_applies_to_merged_result(imap):
    result = _call("email_search", query="DHL", folders=["INBOX", "Archive"], limit=2, include_body=False)
    assert _subjects(result) == ["Old DHL Tracking", "DHL Rechnung"]


def test_local_fallback_matches_every_word(imap):
    imap.reject_search = True
    result = _call("email_search", query="dhl tracking", include_body=False)
    assert _subjects(result) == ["DHL Tracking 123"]
