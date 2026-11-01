"""Tests never read the real Downloads folder: the automatic loaders look in an empty folder unless a test says otherwise.

PERGOLA_STRICT=1 lets errors surface in the tests; the crash-protection tests switch it off to see the friendly messages.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_inbox(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("PERGOLA_INBOX_DIRS", str(tmp_path_factory.mktemp("empty-inbox")))


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("PERGOLA_STRICT", "1")
