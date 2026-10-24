"""Make the Streamlit page installable on a phone ("Add to Home screen" / "Install app").

Streamlit's own page has no web-app manifest, so Chrome would treat it as an ordinary site. When
static file serving is on (`enableStaticServing = true` in .streamlit/config.toml) the files in
./static are served at /app/static/, and this helper links the manifest and icons into the
page's <head>. On Streamlit Community Cloud the app runs inside a wrapper page, so the installed
name and icon can still show "Streamlit"; the `docs/` wrapper site fixes that (see README).
"""
from __future__ import annotations

import json

import streamlit as st

STATIC_BASE = "app/static/"
APP_NAME = "ניהול פרגולות חכם"
THEME_COLOR = "#2a78d6"


def head_script() -> str:
    """JavaScript that adds the manifest / icon tags to the Streamlit page (idempotent)."""
    tags = [
        {"tag": "link", "rel": "manifest", "href": STATIC_BASE + "manifest.json"},
        {"tag": "link", "rel": "apple-touch-icon", "href": STATIC_BASE + "apple-touch-icon.png"},
        {"tag": "meta", "name": "theme-color", "content": THEME_COLOR},
        {"tag": "meta", "name": "mobile-web-app-capable", "content": "yes"},
        {"tag": "meta", "name": "apple-mobile-web-app-capable", "content": "yes"},
        {"tag": "meta", "name": "apple-mobile-web-app-title", "content": "פרגולות"},
    ]
    return f"""<script>
(function () {{
  var doc = window.parent.document, tags = {json.dumps(tags, ensure_ascii=False)};
  tags.forEach(function (t) {{
    var key = t.rel ? 'link[rel="' + t.rel + '"]' : 'meta[name="' + t.name + '"]';
    var el = doc.head.querySelector(key + '[data-pwa]');
    if (!el) {{ el = doc.createElement(t.tag); el.setAttribute('data-pwa', '1'); doc.head.appendChild(el); }}
    Object.keys(t).forEach(function (k) {{
      if (k === 'tag') return;
      el.setAttribute(k, k === 'href' ? new URL(t[k], doc.baseURI).href : t[k]);
    }});
  }});
  doc.title = {json.dumps(APP_NAME, ensure_ascii=False)};
  // Chrome checks installability early, before Streamlit has finished loading, and ignores a
  // manifest link added in that window. Re-attach it once after the page has settled.
  var win = window.parent;
  if (!win.__pwaRetouched) {{
    win.__pwaRetouched = true;
    win.setTimeout(function () {{
      var link = doc.head.querySelector('link[rel="manifest"][data-pwa]');
      if (link) {{ var fresh = link.cloneNode(); link.remove(); doc.head.appendChild(fresh); }}
    }}, 2500);
  }}
}})();
</script>"""


def install_head_tags() -> None:
    """Call once near the top of the app; renders an invisible 1-pixel frame."""
    try:
        st.iframe(head_script(), height=1)
    except Exception:          # never let a cosmetic feature break the app
        pass
