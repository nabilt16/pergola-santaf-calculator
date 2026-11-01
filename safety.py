"""Crash protection: a failure in one part of the app becomes a friendly message, never a white screen.

`guard` wraps one section (loading a file, drawing a tab). `st.stop()` / `st.rerun()` are BaseExceptions in
Streamlit, so they pass through untouched. Set PERGOLA_STRICT=1 (the tests do) to let errors surface instead.
"""
from __future__ import annotations

import os
import traceback
from contextlib import contextmanager

import streamlit as st


def strict() -> bool:
    return os.environ.get("PERGOLA_STRICT", "") not in ("", "0")


def show_failure(label: str, exc: BaseException) -> None:
    """Say what failed, in plain words, with the technical detail folded away."""
    st.error(f"{label}: {exc or type(exc).__name__}. שאר המערכת ממשיכה לעבוד.")
    with st.expander("פרטים טכניים"):
        st.code("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), language="text")


@contextmanager
def guard(label: str):
    try:
        yield
    except Exception as exc:                  # noqa: BLE001 - the whole point is to keep the page alive
        if strict():
            raise
        show_failure(label, exc)


def attempt(label: str, function, default=None):
    """function(), or `default` plus a message when it fails: for a value the rest of the page can do without."""
    try:
        return function()
    except Exception as exc:                  # noqa: BLE001
        if strict():
            raise
        show_failure(label, exc)
        return default
