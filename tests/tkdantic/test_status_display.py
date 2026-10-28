"""Tests for :mod:`ox_ui.tkdantic.status_display`.

Most tests stop the background polling thread from starting (it
calls Tk from a second thread, which needs a running mainloop) and
drive the display directly.  One test runs the real thread and
checks that closing the window stops it.
"""

import logging
import threading
from tkinter import ttk
import tkinter as tk

import pytest

from ox_ui.tkdantic import status_display
from ox_ui.tkdantic.status_display import (
    SimpleStatusDisplay,
    StatusEntry,
    StatusField,
)

from .tk_helpers import find_widgets, text_content

TIMEOUT = 5.0

SERVER = StatusEntry(
    heading='Server',
    rows=(
        StatusField('status: running'),
        StatusField('uptime: 3h', tooltip='Started at 06:38'),
    ),
    heading_tooltip='The main server',
)
DATABASE = ('Database', 'status: error', 'connections: 5')

HIGHLIGHTS = [
    (r'connections: ([1-9]\d*)', {'background': 'lightblue'}),
    (r'error', {'foreground': 'red'}),
    (r'\d+', {'foreground': 'gray'}),
]


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

class ListUpdater:
    """Updater returning a configurable status list."""

    def __init__(self, status=None):
        """Remember the status list to return."""
        self.status = [SERVER, DATABASE] if status is None else status
        self.calls = 0

    def __call__(self):
        """Count the call and return the status list."""
        self.calls += 1
        return self.status


def failing_updater():
    """Updater that always raises."""
    raise RuntimeError('updater exploded')


class StoppingUpdater:
    """Updater that stops its display's poll loop on call two."""

    def __init__(self):
        """Start with no display attached."""
        self.display = None
        self.calls = 0

    def __call__(self):
        """Return a status; on the second call, request stop."""
        self.calls += 1
        if self.calls >= 2:
            self.display._stop_event.set()
        return [('Tick', f'call {self.calls}')]


class BlockingUpdater:
    """Updater that blocks until released, to hold the thread."""

    def __init__(self):
        """Create the called and release events."""
        self.called = threading.Event()
        self.release = threading.Event()

    def __call__(self):
        """Signal the call and wait for release."""
        self.called.set()
        self.release.wait(timeout=TIMEOUT)
        return [('Late', 'never shown')]


class ClickRecorder:
    """on_click callback that records its arguments."""

    def __init__(self):
        """Start with no clicks."""
        self.clicks = []

    def __call__(self, line_text, match_text):
        """Record one click."""
        self.clicks.append((line_text, match_text))


def failing_click(line_text, match_text):
    """on_click callback that always raises."""
    raise RuntimeError('click failed')


class FakeEvent:
    """Minimal stand-in for a Tk mouse event."""

    def __init__(self, x, y):
        """Store widget and screen coordinates."""
        self.x = x
        self.y = y
        self.x_root = x
        self.y_root = y


def no_polling(self):
    """Replacement for _start_polling that starts no thread."""


def fixed_interval(self):
    """Replacement for _safe_interval that avoids Tk calls."""
    return 0.01


@pytest.fixture
def make_display(tk_root, monkeypatch):
    """Return a factory for displays without a polling thread."""
    monkeypatch.setattr(SimpleStatusDisplay, '_start_polling',
                        no_polling)
    return DisplayFactory(tk_root)


class DisplayFactory:
    """Builds SimpleStatusDisplay windows under one root."""

    def __init__(self, root):
        """Remember the root window."""
        self.root = root

    def __call__(self, updater=None, highlights=None):
        """Return a new display for *updater* and *highlights*.

        The window is placed off screen so the Xvfb pointer never
        sends it real Enter events, which would hover-pause it.
        """
        display = SimpleStatusDisplay(
            self.root, title='Status',
            updater=updater or ListUpdater(),
            highlights=highlights)
        display.geometry('+3000+3000')
        return display


def shown(display):
    """Return the text shown in the display."""
    return text_content(display._text)


def ranges(display, tag):
    """Return the text covered by each range of *tag*."""
    spans = display._text.tag_ranges(tag)
    return [display._text.get(spans[i], spans[i + 1])
            for i in range(0, len(spans), 2)]


def line_point(display, index):
    """Return a FakeEvent inside the character at *index*."""
    display.update()
    x, y, _w, h = display._text.bbox(index)
    return FakeEvent(x + 1, y + h // 2)


# ---------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------

class TestNormalize:
    """Tests for :meth:`SimpleStatusDisplay._normalize_entry`."""

    def test_tuple(self):
        """A tuple becomes a heading plus plain rows."""
        entry = SimpleStatusDisplay._normalize_entry(DATABASE)
        assert entry == StatusEntry(
            heading='Database',
            rows=(StatusField('status: error'),
                  StatusField('connections: 5')))

    def test_heading_only_and_empty(self):
        """One-element and empty tuples have no rows."""
        norm = SimpleStatusDisplay._normalize_entry
        assert norm(('Only',)) == StatusEntry(heading='Only')
        assert norm(()) == StatusEntry(heading='')

    def test_entry_unchanged(self):
        """A StatusEntry is returned as is."""
        assert SimpleStatusDisplay._normalize_entry(SERVER) is SERVER


class TestCompileHighlights:
    """Tests for :meth:`SimpleStatusDisplay._compile_highlights`."""

    def test_tags_and_callbacks(self):
        """Each rule gets a tag name and an optional callback."""
        recorder = ClickRecorder()
        compiled = SimpleStatusDisplay._compile_highlights([
            ('a+', {'foreground': 'red'}),
            ('b', {}, recorder),
        ])
        assert [c[1] for c in compiled] == ['_hl_0', '_hl_1']
        assert compiled[0][0].pattern == 'a+'
        assert compiled[0][3] is None
        assert compiled[1][3] is recorder


# ---------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------

class TestRendering:
    """Status lists are rendered into the text area."""

    def test_render_text(self, make_display):
        """Entries become separator, heading and indented rows."""
        display = make_display()
        text, headings, tips = display._render_status(
            [SERVER, DATABASE])
        sep = '-' * 40
        assert text.split('\n') == [
            sep, 'Server', '    status: running', '    uptime: 3h',
            sep, 'Database', '    status: error', '    connections: 5',
        ]
        assert headings == [2, 6]
        assert tips == {2: 'The main server', 4: 'Started at 06:38'}

    def test_empty_heading_only_separator(self, make_display):
        """An entry without a heading renders only a separator."""
        display = make_display()
        text, headings, _tips = display._render_status([(), ('A',)])
        assert text == '-' * 40 + '\n' + '-' * 40 + '\nA'
        assert headings == [3]

    def test_bad_entry_reported(self, make_display, caplog):
        """An entry that cannot be normalised is shown as an error."""
        display = make_display()
        with caplog.at_level(logging.ERROR):
            text, _h, _t = display._render_status([None])
        assert 'error in raw_entry' in text
        assert 'raw_entry=None' in text
        assert 'Unable to normalize' in caplog.text

    def test_unjoinable_parts_reported(self, make_display, caplog):
        """A non-string heading is reported instead of crashing."""
        display = make_display()
        with caplog.at_level(logging.ERROR):
            text, _h, _t = display._render_status([(5, 'x')])
        assert text.startswith('Unable to join parts')

    def test_apply_status(self, make_display):
        """Applying shows the text, bold headings and a timestamp."""
        display = make_display()
        display._apply_status([SERVER, DATABASE])
        assert shown(display).split('\n')[1] == 'Server'
        assert ranges(display, '_heading') == ['Server', 'Database']
        assert str(display._text.cget('state')) == 'disabled'
        label = str(display._timestamp_label.cget('text'))
        assert label.startswith('Last updated: ')
        assert label != 'Last updated: --'

    def test_unchanged_status_not_redrawn(self, make_display):
        """Applying identical status leaves the text widget alone."""
        display = make_display()
        display._apply_status([DATABASE])
        display._text.config(state='normal')
        display._text.insert('end', '\nmarker')
        display._apply_status([DATABASE])
        assert shown(display).endswith('marker')
        display._apply_status([('Changed',)])
        assert 'marker' not in shown(display)

    def test_highlights(self, make_display):
        """Patterns tag matches, or only group 1 if there is one."""
        display = make_display(highlights=HIGHLIGHTS)
        display._apply_status([DATABASE, ('Other', 'connections: 0')])
        assert ranges(display, '_hl_0') == ['5']
        assert ranges(display, '_hl_1') == ['error']
        assert ranges(display, '_hl_2') == ['5', '0']

    def test_highlight_priority(self, make_display):
        """The first rule has the highest priority, headings lowest."""
        display = make_display(highlights=HIGHLIGHTS)
        names = [n for n in display._text.tag_names()
                 if n.startswith('_')]
        assert names == ['_heading', '_hl_2', '_hl_1', '_hl_0']

    def test_updater_error_shown(self, make_display, caplog):
        """Refresh with a failing updater shows the error message."""
        display = make_display(failing_updater, highlights=[
            ('ERROR', {'foreground': 'red'})])
        with caplog.at_level(logging.ERROR):
            display._do_refresh()
        assert shown(display) == (
            '-' * 40 + '\nERROR\n    updater exploded')
        assert ranges(display, '_hl_0') == ['ERROR']
        assert 'Updater raised an exception' in caplog.text


# ---------------------------------------------------------------
# Controls and hover-pause
# ---------------------------------------------------------------

class TestControls:
    """Refresh, Pause and hover-pause behaviour."""

    def test_refresh_button(self, make_display):
        """Refresh calls the updater and shows the result."""
        updater = ListUpdater([('Fresh', 'value')])
        display = make_display(updater)
        find_widgets(display, ttk.Button,
                     'Refresh')[0].invoke()
        assert updater.calls == 1
        assert 'Fresh' in shown(display)

    def test_pause_toggle(self, make_display):
        """Pause stops the poll loop; pressing again resumes it."""
        display = make_display()
        display._pause_btn.invoke()
        assert str(display._pause_btn.cget('text')) == 'Resume'
        assert not display._pause_event.is_set()
        display._pause_btn.invoke()
        assert str(display._pause_btn.cget('text')) == 'Pause'
        assert display._pause_event.is_set()

    def test_hover_holds_updates(self, make_display):
        """While hovering, updates wait; leaving applies the latest."""
        display = make_display()
        display._apply_status([('First',)])
        display._on_text_enter(None)
        assert 'paused' in str(display._hover_label.cget('text'))
        display._apply_status([('Second',)])
        display._apply_status([('Third',)])
        assert 'First' in shown(display)
        display._on_text_leave(None)
        assert 'Third' in shown(display)
        assert str(display._hover_label.cget('text')) == ''

    def test_hover_ignored_when_paused(self, make_display):
        """Hovering while explicitly paused does not hover-pause."""
        display = make_display()
        display._toggle_pause()
        display._on_text_enter(None)
        assert not display._hover_paused
        display._on_text_leave(None)
        assert str(display._hover_label.cget('text')) == ''

    def test_leave_without_pending(self, make_display):
        """Leaving with no pending update keeps the current text."""
        display = make_display()
        display._apply_status([('Kept',)])
        display._on_text_enter(None)
        display._on_text_leave(None)
        assert 'Kept' in shown(display)

    def test_refresh_clears_hover(self, make_display):
        """Refresh drops hover-pause and any pending update."""
        display = make_display(ListUpdater([('Now',)]))
        display._on_text_enter(None)
        display._apply_status([('Stale',)])
        display._do_refresh()
        assert not display._hover_paused
        assert display._pending_status is None
        assert 'Now' in shown(display)


# ---------------------------------------------------------------
# Tooltips and clicks
# ---------------------------------------------------------------

class TestTooltips:
    """Per-line tooltips follow the mouse."""

    def test_motion_shows_and_hides(self, make_display, monkeypatch):
        """Hovering a line with a tooltip shows it; moving hides it."""
        monkeypatch.setattr(status_display, '_TOOLTIP_DELAY_MS', 0)
        display = make_display()
        display._apply_status([SERVER])
        display._on_text_motion(line_point(display, '4.2'))
        display.update()
        tip = display._tooltip_win
        assert tip is not None
        assert find_widgets(tip, tk.Label,
                            'Started at 06:38')
        display._on_text_motion(line_point(display, '4.3'))
        assert display._tooltip_win is tip
        display._on_text_motion(line_point(display, '3.2'))
        assert display._tooltip_win is None
        assert not tip.winfo_exists()

    def test_leave_cancels_timer(self, make_display):
        """Leaving the text area cancels a pending tooltip."""
        display = make_display()
        display._apply_status([SERVER])
        display._on_text_motion(line_point(display, '2.1'))
        assert display._tooltip_after_id is not None
        display._on_text_leave(None)
        assert display._tooltip_after_id is None
        assert display._tooltip_win is None

    def test_close_hides_tooltip(self, make_display):
        """Closing the window removes a visible tooltip."""
        display = make_display()
        display._show_tooltip('tip', 2, 10, 10)
        tip = display._tooltip_win
        display._on_close()
        assert not tip.winfo_exists()
        assert display._stop_event.is_set()
        assert not display.winfo_exists()


class TestClicks:
    """Clickable highlight tags call their callbacks."""

    def test_click_callback(self, make_display):
        """A click passes the line and the tagged text."""
        recorder = ClickRecorder()
        display = make_display(highlights=[
            (r'connections: (\d+)', {'underline': True}, recorder)])
        display._apply_status([DATABASE])
        event = line_point(display, '4.17')
        display._on_tag_click(event, recorder, '_hl_0')
        assert recorder.clicks == [('    connections: 5', '5')]

    def test_click_events_bound(self, make_display):
        """Real mouse events change the cursor and fire the click."""
        recorder = ClickRecorder()
        display = make_display(highlights=[
            (r'error', {'foreground': 'red'}, recorder)])
        display._apply_status([DATABASE])
        event = line_point(display, '3.13')
        text = display._text
        text.event_generate('<Enter>', x=event.x, y=event.y)
        text.event_generate('<Motion>', x=event.x, y=event.y)
        assert str(text.cget('cursor')) == 'hand2'
        text.event_generate('<Button-1>', x=event.x, y=event.y)
        text.event_generate('<ButtonRelease-1>', x=event.x, y=event.y)
        assert recorder.clicks == [('    status: error', 'error')]
        far = line_point(display, '1.0')
        text.event_generate('<Motion>', x=far.x, y=far.y)
        assert str(text.cget('cursor')) == display._default_cursor

    def test_click_outside_tag(self, make_display):
        """A click with no tagged range passes an empty match."""
        recorder = ClickRecorder()
        display = make_display()
        display._apply_status([DATABASE])
        display._on_tag_click(line_point(display, '1.0'), recorder,
                              '_hl_9')
        assert recorder.clicks == [('-' * 40, '')]

    def test_click_error_logged(self, make_display, caplog):
        """An exception in a click callback is logged."""
        display = make_display()
        display._apply_status([DATABASE])
        with caplog.at_level(logging.ERROR):
            display._on_tag_click(line_point(display, '2.0'),
                                  failing_click, '_hl_0')
        assert 'on_click callback' in caplog.text


# ---------------------------------------------------------------
# Polling
# ---------------------------------------------------------------

class TestPolling:
    """The background poll loop and its interval."""

    def test_poll_loop(self, make_display):
        """The loop applies results via after_idle until stopped."""
        updater = StoppingUpdater()
        display = make_display(updater)
        updater.display = display
        display._interval_var.set(0.01)
        display._poll_loop()
        assert updater.calls == 2
        display.update()
        assert 'call 1' in shown(display)

    @pytest.mark.parametrize('value, expected', [
        (0.5, 0.5),
        (0.0, 0.01),
        ('abc', 0.2),
    ])
    def test_safe_interval(self, make_display, value, expected):
        """The interval is clamped and falls back to 0.2 s."""
        display = make_display()
        display.setvar(str(display._interval_var), value)
        assert display._safe_interval() == expected

    def test_thread_stops_on_close(self, tk_root, monkeypatch):
        """Closing the window ends the real polling thread."""
        monkeypatch.setattr(SimpleStatusDisplay, '_safe_interval',
                            fixed_interval)
        updater = BlockingUpdater()
        display = SimpleStatusDisplay(tk_root, title='T',
                                      updater=updater)
        assert updater.called.wait(TIMEOUT)
        display._on_close()
        updater.release.set()
        display._thread.join(timeout=TIMEOUT)
        assert not display._thread.is_alive()
