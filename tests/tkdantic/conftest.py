"""Shared fixtures for the tkdantic tests.

GUI tests request the ``tk_root`` fixture.  When no display is
available (for example on a CI runner without Xvfb) creating the
root raises :class:`tkinter.TclError` and the test is skipped.
"""

import tkinter

import pytest


@pytest.fixture
def tk_root():
    """Yield a withdrawn Tk root and destroy it after the test.

    Skips the test when Tk cannot open a display.
    """
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f'No display available for Tk: {exc}')
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tkinter.TclError:
        pass
