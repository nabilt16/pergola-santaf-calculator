"""Tests never read the real Downloads folder: the automatic loaders look in an empty folder unless a test says otherwise."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_inbox(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("PERGOLA_INBOX_DIRS", str(tmp_path_factory.mktemp("empty-inbox")))
