"""Which version is running, and where does an upload stop? (temporary diagnostics, nothing leaves the page)

* `version()` reads the commit of the running copy from the .git folder (no git program needed), so the footer says
  which version a phone is really looking at.
* The SERVER side keeps a short list of stages per session: ready for a file, file received (size only), reading,
  rows extracted, checked against the printed totals, shown, or the kind of failure. No file names, no order data.
* The BROWSER side (a small script that watches the page, passively) records what only the phone can see: the file
  picker closing and the page coming back, the file input receiving a file (count and size only), the upload request
  to the server and its HTTP status, error text appearing in the uploader, the connection dropping / returning, and
  network changes. It writes into the panel itself and into the browser's sessionStorage. Nothing is sent anywhere.

The panel is a closed expander at the bottom of the page: "אבחון העלאה". Open it after a failed attempt and send a
screenshot (or the copied text).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import streamlit as st

EVENTS = "diag_events"
MAX_EVENTS = 40
_ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- the running version
def _git_dir(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        git = folder / ".git"
        if git.is_dir():
            return git
        if git.is_file():                                   # a worktree: "gitdir: <path>"
            try:
                target = git.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()
                return (folder / target).resolve()
            except Exception:
                return None
    return None


def version() -> str:
    """The short commit of the running copy, or an environment value, or 'unknown'. Never raises."""
    for name in ("APP_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        if os.environ.get(name):
            return os.environ[name][:7]
    try:
        git = _git_dir(_ROOT)
        if git is None:
            return "unknown"
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:7]
        ref = head.split(":", 1)[1].strip()
        ref_file = git / ref
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip()[:7]
        common = git / "commondir"                          # a worktree keeps refs in the main git folder
        if common.is_file():
            main = (git / common.read_text(encoding="utf-8").strip()).resolve()
            if (main / ref).is_file():
                return (main / ref).read_text(encoding="utf-8").strip()[:7]
            git = main
        packed = git / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0][:7]
    except Exception:
        pass
    return "unknown"


@st.cache_resource(show_spinner=False)
def _process() -> dict:
    """Lives as long as the server process: shows a restart (sleep and wake-up) between two attempts."""
    return {"started": time.time(), "sessions": 0}


# --------------------------------------------------------------------------- server stages
def mark(stage: str, detail: str = "") -> None:
    """Record a stage of the current session. `detail` must be harmless (a count or a size), never data."""
    try:
        events = st.session_state.setdefault(EVENTS, [])
        if events and events[-1][1] == stage and events[-1][2] == detail:
            return                                           # a rerun that changes nothing
        events.append((time.strftime("%H:%M:%S"), stage, detail))
        del events[:-MAX_EVENTS]
    except Exception:
        pass


def mark_files(files) -> None:
    """The uploader's content as the server sees it: how many files, how big (never the names)."""
    try:
        files = list(files or [])
        if not files:
            mark("ready", "no file in the uploader")
        else:
            mark("received", f"{len(files)} file(s), {sum(len(f.getvalue()) for f in files) // 1024} KB")
    except Exception:
        pass


# --------------------------------------------------------------------------- the panel
_JS = r"""
<div id="p" style="font:12px/1.5 monospace;direction:ltr;white-space:pre-wrap;word-break:break-all"></div>
<button id="c" style="margin-top:6px;padding:6px 12px">copy</button>
<script>
(function () {
  var VIEW = __VIEW__, P = window.parent, D = P.document, KEY = 'pergolaDiag', out = VIEW ? document.getElementById('p') : null;
  function load() { try { return JSON.parse(P.sessionStorage.getItem(KEY) || '[]'); } catch (e) { return []; } }
  function save(l) { try { P.sessionStorage.setItem(KEY, JSON.stringify(l.slice(-60))); } catch (e) {} }
  function log(what) {
    var l = load(), t = new Date(), s = ('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2)+':'+('0'+t.getSeconds()).slice(-2);
    l.push(s + ' ' + what); save(l); if (out) draw();
  }
  function draw() { out.textContent = load().join('\n') || '(nothing recorded yet)'; }
  if (!P.__pergolaDiag) {
    P.__pergolaDiag = true;
    log('monitor started; ' + (navigator.connection ? navigator.connection.effectiveType : 'net?') + '; online=' + navigator.onLine);
    D.addEventListener('visibilitychange', function () { log('page ' + D.visibilityState); });
    P.addEventListener('online', function () { log('network online'); });
    P.addEventListener('offline', function () { log('network OFFLINE'); });
    D.addEventListener('change', function (e) {
      var t = e.target;
      if (t && t.type === 'file') {
        var n = t.files ? t.files.length : 0, kb = 0;
        for (var i = 0; i < n; i++) kb += Math.round(t.files[i].size / 1024);
        log('file input changed: ' + n + ' file(s), ' + kb + ' KB' + (n === 0 ? '  <-- NO FILE RECEIVED' : ''));
      }
    }, true);
    try {
      new P.PerformanceObserver(function (list) {
        list.getEntries().forEach(function (en) {
          if (/upload_file/.test(en.name)) {
            log('upload request: HTTP ' + (en.responseStatus || '?') + ' in ' + Math.round(en.duration) + ' ms');
          }
        });
      }).observe({type: 'resource', buffered: false});
    } catch (e) { log('no resource timing'); }
    var last = '', pending = false;
    function look() {                       // at most 3 looks a second: Streamlit changes the page a lot, a phone must not feel it
      pending = false;
      var up = D.querySelector('[data-testid="stFileUploader"]');
      var state = 'uploader=' + (up ? 'shown' : 'absent');
      if (up) {
        var txt = up.innerText || '';
        if (/error|failed|שגיאה|נכשל|too large|גדול/i.test(txt)) state += ' ERROR-TEXT';
        state += ' chips=' + up.querySelectorAll('[data-testid="stFileChip"]').length;
      }
      var conn = D.querySelector('[data-testid="stConnectionStatus"]');
      if (conn && /connect/i.test(conn.innerText || '')) state += ' CONNECTION-LOST';
      if (D.querySelector('[data-testid="stStatusWidget"]')) state += ' running';
      var ok = false, alerts = D.querySelectorAll('[data-testid="stAlert"]');
      for (var i = 0; i < alerts.length && i < 20; i++) if ((alerts[i].innerText || '').indexOf('נקלט בהצלחה') >= 0) ok = true;
      if (ok) state += ' SUCCESS-MESSAGE-SHOWN';
      if (state !== last) { last = state; log(state); }
    }
    new P.MutationObserver(function () {
      if (!pending) { pending = true; setTimeout(look, 330); }
    }).observe(D.body, {childList: true, subtree: true, characterData: true});
  }
  if (!VIEW) return;
  draw();
  document.getElementById('c').onclick = function () {
    var ta = document.createElement('textarea'); ta.value = load().join('\n'); document.body.appendChild(ta);
    ta.select(); try { document.execCommand('copy'); } catch (e) {} document.body.removeChild(ta);
  };
  setInterval(draw, 1500);
})();
</script>
"""


def _frame(html: str, height: int) -> None:
    """An iframe with a script in it (st.iframe; the older components.html where st.iframe does not exist yet)."""
    if hasattr(st, "iframe"):
        st.iframe(html.strip(), height=height)
    else:
        import streamlit.components.v1 as components
        components.html(html, height=height)


def monitor() -> None:
    """Start the passive browser-side recording (an invisible frame). Never raises."""
    try:
        _frame(_JS.replace("__VIEW__", "false"), 1)
    except Exception:
        pass


def footer() -> None:
    """A small line with the version and the closed diagnostics panel. Never raises."""
    try:
        proc = _process()
        if not st.session_state.get("diag_counted"):
            st.session_state["diag_counted"] = True
            proc["sessions"] += 1
        st.caption(f"גרסה {version()}")
        with st.expander("אבחון העלאה"):
            up = int(time.time() - proc["started"])
            st.caption(f"השרת פועל {up // 3600} שעות {up % 3600 // 60} דקות · סשנים מאז ההפעלה: {proc['sessions']} · "
                       f"גרסה {version()}")
            events = st.session_state.get(EVENTS, [])
            st.code("\n".join(f"{t} {s} {d}".strip() for t, s, d in events) or "(אין אירועים בשרת עדיין)", language=None)
            st.caption("מה שהדפדפן ראה (ללא שמות קבצים ותוכן):")
            _frame(_JS.replace("__VIEW__", "true"), 330)
    except Exception:
        pass


def summary() -> str:
    return json.dumps({"version": version(), "events": st.session_state.get(EVENTS, [])}, ensure_ascii=False)
