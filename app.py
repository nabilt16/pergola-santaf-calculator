"""Entry point of "ניהול פרגולות חכם". Run with:  streamlit run app.py

The real application is main_app.py. This thin file runs it inside a safety net, so an error while it starts
(a missing or broken file, a bad import, a corrupt saved file) shows a clear message instead of a white screen.
"""
import runpy
import traceback
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent


def _report(exc: BaseException) -> None:
    st.error(f"האפליקציה נתקלה בשגיאה: {exc or type(exc).__name__}. אפשר לרענן את הדף ולנסות שוב.")
    with st.expander("פרטים טכניים"):
        st.code("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), language="text")


try:
    import safety
    strict = safety.strict()
except Exception:                                  # even a broken safety.py must not stop the app from starting
    strict = False

try:
    runpy.run_path(str(HERE / "main_app.py"), run_name="__main__")
except Exception as exc:                           # st.stop() / st.rerun() are BaseExceptions and pass through
    if strict:
        raise
    _report(exc)
