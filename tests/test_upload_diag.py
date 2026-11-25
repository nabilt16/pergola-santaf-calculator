"""Version stamp and upload diagnostics: the commit is found without git, stages are recorded without any data."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import upload_diag

SHA = "0123456789abcdef0123456789abcdef01234567"


def _repo(tmp_path, head, refs=None, packed=None):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(head, encoding="utf-8")
    for name, value in (refs or {}).items():
        (git / name).parent.mkdir(parents=True, exist_ok=True)
        (git / name).write_text(value + "\n", encoding="utf-8")
    if packed:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for name in ("APP_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        monkeypatch.delenv(name, raising=False)


def _version_of(monkeypatch, folder):
    monkeypatch.setattr(upload_diag, "_ROOT", Path(folder))
    return upload_diag.version()


def test_the_commit_is_read_from_a_loose_branch_ref(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "ref: refs/heads/main\n", {"refs/heads/main": SHA})
    assert _version_of(monkeypatch, repo) == SHA[:7]


def test_the_commit_is_read_from_packed_refs(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "ref: refs/heads/main\n", packed=f"# pack-refs\n{SHA} refs/heads/main\n")
    assert _version_of(monkeypatch, repo) == SHA[:7]


def test_the_commit_of_a_detached_head(tmp_path, monkeypatch):
    assert _version_of(monkeypatch, _repo(tmp_path, SHA + "\n")) == SHA[:7]


def test_the_commit_is_found_from_a_subfolder_and_a_worktree(tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    _repo(main, "ref: refs/heads/main\n", {"refs/heads/main": SHA})
    work = tmp_path / "work"
    (main / ".git" / "worktrees" / "w").mkdir(parents=True)
    (main / ".git" / "worktrees" / "w" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (main / ".git" / "worktrees" / "w" / "commondir").write_text("../..\n", encoding="utf-8")
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'w'}\n", encoding="utf-8")
    sub = work / "a" / "b"
    sub.mkdir(parents=True)
    assert _version_of(monkeypatch, sub) == SHA[:7]


def test_no_git_folder_gives_unknown_and_never_raises(tmp_path, monkeypatch):
    assert _version_of(monkeypatch, tmp_path / "nowhere") == "unknown"
    assert _version_of(monkeypatch, _repo(tmp_path, "ref: refs/heads/gone\n")) == "unknown"


def test_an_environment_value_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_COMMIT", "abcdef1234")
    assert _version_of(monkeypatch, tmp_path) == "abcdef1"


class _File:
    def __init__(self, name, size):
        self.name, self._data = name, b"x" * size

    def getvalue(self):
        return self._data


def _run(fn):
    at = AppTest.from_function(fn, default_timeout=30)
    at.run()
    assert not at.exception
    return at


def test_stages_are_recorded_once_and_without_names_or_data():
    def app():
        import streamlit as st
        import upload_diag as d

        class F:
            name = "דוח של לקוח סודי.pdf"

            def getvalue(self):
                return b"%PDF" + b"0" * 4096
        d.mark_files([])
        d.mark_files([F()])
        d.mark_files([F()])
        d.mark("reading")
        st.session_state["events"] = list(st.session_state["diag_events"])
    at = _run(app)
    events = at.session_state["events"]
    assert [e[1] for e in events] == ["ready", "received", "reading"]               # the repeat is not recorded twice
    flat = " ".join(" ".join(e) for e in events)
    assert "סודי" not in flat and ".pdf" not in flat
    assert "1 file(s), 4 KB" in flat


def test_the_event_list_is_capped():
    def app():
        import streamlit as st
        import upload_diag as d
        for i in range(200):
            d.mark("stage", str(i))
        st.session_state["n"] = len(st.session_state["diag_events"])
    assert _run(app).session_state["n"] == upload_diag.MAX_EVENTS


def test_the_footer_shows_the_version_and_a_closed_panel_and_never_breaks_the_page():
    def app():
        import upload_diag as d
        d.monitor()
        d.footer()
    at = _run(app)
    assert any("גרסה" in c.value for c in at.caption)
    assert [e.label for e in at.expander] == ["אבחון העלאה"]


def test_the_browser_script_records_no_file_names_or_contents():
    script = upload_diag._JS
    assert ".name" not in script.replace("en.name", "")                              # only the request address is tested, never printed
    assert "file input changed" in script and "upload request: HTTP" in script
    assert "fetch(" not in script and "XMLHttpRequest" not in script and "sendBeacon" not in script   # nothing is sent anywhere


def test_the_older_streamlit_without_st_iframe_still_gets_the_panel(monkeypatch):
    def app():
        import streamlit as st
        import upload_diag as d
        if hasattr(st, "iframe"):
            del st.iframe                                   # as in a Streamlit that does not have it yet
        d.monitor()
        d.footer()
    at = _run(app)
    assert any("גרסה" in c.value for c in at.caption)


def test_the_uploader_keeps_its_file_across_reruns_with_the_diagnostics_on(tmp_path, monkeypatch):
    """The diagnostics must not change the uploader key: the same file stays after a rerun (a click elsewhere)."""
    import test_upload as t
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=60)
    at.run()
    keys_before = [u.key for u in at.file_uploader]
    at.file_uploader[0].set_value([t._pdf(tmp_path)]).run()
    assert [u.key for u in at.file_uploader] == keys_before
    at.run()                                                # an unrelated rerun
    assert [u.key for u in at.file_uploader] == keys_before
    assert len(at.file_uploader[0].value or []) == 1 and not at.exception
