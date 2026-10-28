"""Tests for :class:`StateRunnerWindow` and :class:`DiagramWindow`.

The windows are driven by calling their button commands and poll
methods directly; ``mainloop()`` is never entered.  Worker threads
are synchronised with :class:`threading.Event` objects and joined
with timeouts.  Dialogs are replaced with :class:`ConstantFn`.
"""

import logging
import os
import threading
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from pydantic import BaseModel
import pytest
from transitions import Machine

from ox_ui.tkdantic.runnable import RunnableMachineHelper
from ox_ui.tkdantic.state_runner import (
    DiagramWindow,
    StateRunner,
    StateRunnerWindow,
    format_status_text,
)

from .tk_helpers import (
    ConstantFn,
    find_widgets,
    is_packed,
    pending_after_scripts,
    text_content,
)

TIMEOUT = 5.0


# ---------------------------------------------------------------
# Machines
# ---------------------------------------------------------------

class Gate(RunnableMachineHelper):
    """Machine whose ``start`` transition waits for a release.

    ``entered`` is set once the worker is inside the transition;
    the transition then waits on ``release`` before finishing.
    """

    def __init__(self):
        """Build an idle -> running -> done machine."""
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._machine = Machine(
            model=self,
            states=['idle', 'running', 'done'],
            transitions=[
                {'trigger': 'start', 'source': 'idle',
                 'dest': 'running'},
                {'trigger': 'finish', 'source': 'running',
                 'dest': 'done'},
            ],
            initial='idle',
        )

    def get_machine(self):
        """Return the transitions Machine."""
        return self._machine

    def on_enter_running(self):
        """Report half progress, wait for release, then finish."""
        self.set_progress(0.5)
        self.set_status('halfway')
        self.entered.set()
        self.release.wait(timeout=TIMEOUT)
        self.wait_if_paused()
        if self.is_cancelled():
            self.set_status('Cancelled.')
            return
        self.set_progress(1.0)
        self.set_status('finished')


class Broken(RunnableMachineHelper):
    """Machine whose only transition raises."""

    def __init__(self):
        """Build an A -> B machine."""
        super().__init__()
        self._machine = Machine(
            model=self, states=['A', 'B'],
            transitions=[{'trigger': 'go', 'source': 'A',
                          'dest': 'B'}],
            initial='A',
        )

    def get_machine(self):
        """Return the transitions Machine."""
        return self._machine

    def on_enter_B(self):
        """Fail on purpose."""
        raise RuntimeError('boom')


class Stuck(RunnableMachineHelper):
    """Machine with one state and no triggers."""

    def __init__(self):
        """Build a single-state machine."""
        super().__init__()
        self._machine = Machine(model=self, states=['only'],
                                initial='only')

    def get_machine(self):
        """Return the transitions Machine."""
        return self._machine


class Forked(RunnableMachineHelper):
    """Machine with two triggers that prefers a configurable one."""

    def __init__(self, preferred=None):
        """Build the machine; *preferred* is returned by the hook."""
        super().__init__()
        self.preferred = preferred
        self._machine = Machine(
            model=self, states=['idle', 'left', 'right'],
            transitions=[
                {'trigger': 'go_left', 'source': 'idle',
                 'dest': 'left'},
                {'trigger': 'go_right', 'source': 'idle',
                 'dest': 'right'},
            ],
            initial='idle',
        )

    def get_machine(self):
        """Return the transitions Machine."""
        return self._machine

    def get_preferred_trigger(self):
        """Return the configured preference, raising if asked."""
        if self.preferred == 'raise':
            raise RuntimeError('no preference today')
        return self.preferred


class Knobs(BaseModel):
    """State variables for :class:`Tunable`."""

    speed: int = 1
    label: str = 'slow'


class Tunable(Gate):
    """Gate machine with user-editable state variables."""

    def __init__(self):
        """Start with default Knobs."""
        super().__init__()
        self.set_state_variables(Knobs(speed=2, label='warm'))


class EscapeHatch(Exception):
    """Raised by the fake ``os._exit`` so tests can catch it."""


def fake_exit(code):
    """Stand-in for :func:`os._exit` that raises instead."""
    raise EscapeHatch(code)


def raise_error():
    """Callable that always fails."""
    raise RuntimeError('poll failed')


MAINLOOP_WINDOWS = []


def fake_mainloop(self, n=0):
    """Stand-in for ``mainloop`` that records the window."""
    MAINLOOP_WINDOWS.append(self)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def make_window(tk_root, machine, **kwargs):
    """Return a StateRunnerWindow driving *machine*."""
    return StateRunnerWindow(tk_root, StateRunner(machine), **kwargs)


def status_of(window):
    """Return the text shown in the window's status pane."""
    return text_content(window._status_text)


def state_of(widget):
    """Return a widget's ``state`` option as a string."""
    return str(widget.cget('state'))


def finish_transition(window):
    """Wait for the worker thread, then run one poll."""
    window._runner._worker_thread.join(timeout=TIMEOUT)
    assert not window._runner.is_busy()
    window._poll_inner()


def start_gate(tk_root):
    """Open a window on a Gate and start its transition."""
    gate = Gate()
    window = make_window(tk_root, gate)
    window._go_btn.invoke()
    assert gate.entered.wait(TIMEOUT)
    return gate, window


def patch_dialog(monkeypatch, name, value):
    """Replace ``filedialog.<name>`` with a ConstantFn."""
    fake = ConstantFn(value)
    monkeypatch.setattr(filedialog, name, fake)
    return fake


# ---------------------------------------------------------------
# Construction and appearance
# ---------------------------------------------------------------

class TestConstruction:
    """The window shows the machine's state and triggers."""

    def test_initial_display(self, tk_root):
        """State, triggers and button states after construction."""
        window = make_window(tk_root, Gate())
        assert window.title() == 'State Runner'
        assert str(window._state_label.cget('text')) == (
            'Current: idle')
        assert str(window._all_states_label.cget('text')) == (
            '[idle]  running  done')
        assert window._trigger_var.get() == 'start'
        assert state_of(window._go_btn) == 'normal'
        for btn in (window._pause_btn, window._resume_btn,
                    window._cancel_btn):
            assert state_of(btn) == 'disabled'
        assert not window._has_state_vars

    def test_no_triggers(self, tk_root):
        """With no triggers, Go is disabled and does nothing."""
        machine = Stuck()
        window = make_window(tk_root, machine)
        assert window._trigger_var.get() == ''
        assert state_of(window._go_btn) == 'disabled'
        window._on_go()
        assert not window._runner.is_busy()

    def test_trigger_menu_selects(self, tk_root):
        """Picking a trigger from the dropdown selects it."""
        window = make_window(tk_root, Forked())
        assert window._trigger_var.get() == 'go_left'
        window._trigger_menu['menu'].invoke(1)
        assert window._trigger_var.get() == 'go_right'

    @pytest.mark.parametrize('preferred, expected', [
        ('go_right', 'go_right'),
        ('not_a_trigger', 'go_left'),
        (None, 'go_left'),
        ('raise', 'go_left'),
    ])
    def test_preferred_trigger(self, tk_root, preferred, expected):
        """The preferred trigger is selected only if available."""
        window = make_window(tk_root, Forked(preferred))
        assert window._trigger_var.get() == expected

    def test_format_status_text(self):
        """The formatting hook returns the text unchanged."""
        assert format_status_text('a\nb') == 'a\nb'


class TestAppearance:
    """Nickname and accent color banner."""

    def test_plain(self, tk_root):
        """Without nickname or accent there is no banner or stripe."""
        window = make_window(tk_root, Gate(), title='Runner')
        assert window.title() == 'Runner'
        assert not is_packed(window._banner_frame)
        assert not is_packed(window._bottom_stripe)

    def test_nickname(self, tk_root):
        """A nickname shows a tall banner and prefixes the title."""
        window = make_window(tk_root, Gate(), title='Runner',
                             nickname='Alpha')
        assert window.title() == 'Alpha — Runner'
        assert window.pack_slaves()[0] == window._banner_frame
        assert str(window._banner_label.cget('text')) == 'Alpha'
        assert is_packed(window._banner_label)
        assert int(window._banner_frame.cget('height')) == 34
        assert not is_packed(window._bottom_stripe)

    def test_accent_only(self, tk_root):
        """An accent alone gives a thin colored stripe at both ends."""
        window = make_window(tk_root, Gate(), accent_color='Red')
        assert window.pack_slaves()[0] == window._banner_frame
        assert str(window._banner_frame.cget('bg')) == '#d64545'
        assert int(window._banner_frame.cget('height')) == 6
        assert not is_packed(window._banner_label)
        assert is_packed(window._bottom_stripe)
        assert str(window._bottom_stripe.cget('bg')) == '#d64545'

    def test_unknown_accent_ignored(self, tk_root):
        """An unknown accent name means no accent."""
        window = make_window(tk_root, Gate(), accent_color='Pink')
        assert window._current_accent == 'None'
        assert not is_packed(window._banner_frame)

    def test_nickname_commit(self, tk_root):
        """Committing a nickname updates the title; blank clears it."""
        window = make_window(tk_root, Gate(), title='Runner')
        window._nickname_var.set('  Beta ')
        window._on_nickname_commit()
        assert window.title() == 'Beta — Runner'
        assert is_packed(window._banner_frame)
        window._on_nickname_commit()
        window._nickname_var.set('')
        window._on_nickname_commit()
        assert window.title() == 'Runner'
        assert not is_packed(window._banner_frame)
        assert not is_packed(window._banner_label)

    def test_accent_change(self, tk_root):
        """Changing the accent recolors; unknown values are ignored."""
        window = make_window(tk_root, Gate(), nickname='N',
                             accent_color='Yellow')
        assert str(window._banner_label.cget('fg')) == 'black'
        window._on_accent_changed('Blue')
        assert str(window._banner_frame.cget('bg')) == '#3f7fd6'
        assert str(window._banner_label.cget('fg')) == 'white'
        window._on_accent_changed('Blue')
        window._on_accent_changed('Mauve')
        assert window._current_accent == 'Blue'
        window._on_accent_changed('None')
        assert not is_packed(window._bottom_stripe)
        assert is_packed(window._banner_frame)

    @pytest.mark.parametrize('color, expected', [
        ('#ffffff', 'black'),
        ('#000000', 'white'),
        ('#e6c84a', 'black'),
        ('#3f7fd6', 'white'),
        ('red', 'black'),
        ('#12', 'black'),
    ])
    def test_contrasting_text_color(self, color, expected):
        """Text color is chosen by luminance; bad input is black."""
        assert StateRunnerWindow._contrasting_text_color(color) == (
            expected)


# ---------------------------------------------------------------
# Running transitions
# ---------------------------------------------------------------

class TestTransitions:
    """Go, poll, pause, resume, cancel and errors."""

    def test_go_and_finish(self, tk_root):
        """A full transition updates controls, progress and state."""
        gate, window = start_gate(tk_root)
        assert window._runner.is_busy()
        assert state_of(window._go_btn) == 'disabled'
        assert state_of(window._pause_btn) == 'normal'
        assert state_of(window._cancel_btn) == 'normal'
        assert status_of(window) == 'Running: start'

        window._poll_inner()
        assert str(window._progress_bar.cget('mode')) == 'determinate'
        assert float(window._progress_bar.cget('value')) == 50
        assert status_of(window) == 'halfway'

        gate.release.set()
        finish_transition(window)
        assert status_of(window) == 'finished'
        assert str(window._state_label.cget('text')) == (
            'Current: running')
        assert window._trigger_var.get() == 'finish'
        assert state_of(window._go_btn) == 'normal'
        assert state_of(window._pause_btn) == 'disabled'
        assert float(window._progress_bar.cget('value')) == 0

    def test_pause_and_resume(self, tk_root):
        """Pause and Resume toggle the buttons and the controller."""
        gate, window = start_gate(tk_root)
        window._pause_btn.invoke()
        assert state_of(window._pause_btn) == 'disabled'
        assert state_of(window._resume_btn) == 'normal'
        assert not gate.controller._pause_event.is_set()
        window._resume_btn.invoke()
        assert gate.controller._pause_event.is_set()
        assert state_of(window._pause_btn) == 'normal'
        assert state_of(window._resume_btn) == 'disabled'
        gate.release.set()
        finish_transition(window)
        assert status_of(window) == 'finished'

    def test_cancel(self, tk_root):
        """Cancel asks the machine to stop and reports it."""
        gate, window = start_gate(tk_root)
        window._cancel_btn.invoke()
        assert status_of(window).startswith('Cancel requested')
        assert state_of(window._cancel_btn) == 'disabled'
        assert gate.is_cancelled()
        gate.release.set()
        finish_transition(window)
        assert status_of(window) == 'Cancelled.'

    def test_error_shown(self, tk_root):
        """An exception in the transition is shown in the status."""
        window = make_window(tk_root, Broken())
        window._on_go()
        finish_transition(window)
        assert status_of(window) == 'ERROR during transition: boom'

    def test_busy_runner_refuses_go(self, tk_root):
        """Go while a transition runs does not start another one."""
        gate, window = start_gate(tk_root)
        window._update_status_pane('unchanged')
        assert not window._runner.start_transition('finish')
        window._on_go()
        assert status_of(window) == 'unchanged'
        gate.release.set()
        finish_transition(window)

    def test_idle_poll_does_nothing(self, tk_root):
        """Polling an idle window leaves the status pane alone."""
        window = make_window(tk_root, Gate())
        window._update_status_pane('keep')
        window._poll_inner()
        assert status_of(window) == 'keep'

    def test_progress_bar_modes(self, tk_root):
        """None progress is indeterminate; stop resets to zero."""
        window = make_window(tk_root, Gate())
        window._update_progress_bar(0.25)
        assert str(window._progress_bar.cget('mode')) == 'determinate'
        assert float(window._progress_bar.cget('value')) == 25
        window._update_progress_bar(None)
        assert str(window._progress_bar.cget('mode')) == (
            'indeterminate')
        window._stop_progress_bar()
        assert str(window._progress_bar.cget('mode')) == 'determinate'
        assert float(window._progress_bar.cget('value')) == 0


class TestReturnKey:
    """Enter fires Go unless typing in an entry or Go is disabled."""

    def test_fires_go(self, tk_root, monkeypatch):
        """Enter with focus on a button starts the transition."""
        gate = Gate()
        window = make_window(tk_root, gate)
        monkeypatch.setattr(window, 'focus_get',
                            ConstantFn(window._go_btn))
        window._on_return_key(None)
        assert gate.entered.wait(TIMEOUT)
        gate.release.set()
        finish_transition(window)

    def test_ignored_in_entry(self, tk_root, monkeypatch):
        """Enter in an entry widget does not fire Go."""
        window = make_window(tk_root, Gate())
        entry = ttk.Entry(window)
        monkeypatch.setattr(window, 'focus_get', ConstantFn(entry))
        window._on_return_key(None)
        assert not window._runner.is_busy()

    def test_ignored_when_disabled(self, tk_root, monkeypatch):
        """Enter does nothing while Go is disabled."""
        window = make_window(tk_root, Stuck())
        monkeypatch.setattr(window, 'focus_get', ConstantFn(None))
        window._trigger_var.set('ghost')
        window._on_return_key(None)
        assert not window._runner.is_busy()


# ---------------------------------------------------------------
# Other buttons
# ---------------------------------------------------------------

class TestButtons:
    """Abort, Diagram and log-file buttons."""

    def test_abort_declined(self, tk_root, monkeypatch):
        """Declining the abort prompt keeps the program running."""
        ask = ConstantFn(False)
        monkeypatch.setattr(messagebox, 'askyesno', ask)
        monkeypatch.setattr(os, '_exit', fake_exit)
        window = make_window(tk_root, Gate())
        window._abort_btn.invoke()
        assert len(ask.calls) == 1

    def test_abort_confirmed(self, tk_root, monkeypatch, tmp_path):
        """Confirming the abort closes the log and exits with 1."""
        monkeypatch.setattr(messagebox, 'askyesno', ConstantFn(True))
        monkeypatch.setattr(os, '_exit', fake_exit)
        window = make_window(tk_root, Gate())
        window._open_log_file(str(tmp_path / 'status.log'))
        with pytest.raises(EscapeHatch) as excinfo:
            window._on_abort()
        assert excinfo.value.args == (1,)
        assert window._log_file_handle is None

    def test_diagram(self, tk_root):
        """Diagram lists states, marks the current one and edges."""
        window = make_window(tk_root, Gate())
        window._diagram_btn.invoke()
        diagrams = find_widgets(window, DiagramWindow)
        assert len(diagrams) == 1
        text = text_content(diagrams[0]._text)
        assert '  -->  idle' in text
        assert '       running' in text
        assert '  idle --start--> running' in text
        assert '  running --finish--> done' in text
        assert 'to_idle' not in text

    def test_log_file(self, tk_root, monkeypatch, tmp_path):
        """Browse picks a log file; changed statuses are appended."""
        path = tmp_path / 'status.log'
        patch_dialog(monkeypatch, 'asksaveasfilename', str(path))
        window = make_window(tk_root, Gate())
        find_widgets(window, ttk.Button, 'Browse...')[0].invoke()
        assert window._log_path_var.get() == str(path)
        window._maybe_log_status('one')
        window._maybe_log_status('one')
        window._maybe_log_status('two')
        window._close_log_file()
        lines = path.read_text(encoding='utf-8').splitlines()
        assert [ln.split(' | ', 1)[1] for ln in lines] == [
            'one', 'two']

    def test_transition_logged(self, tk_root, tmp_path):
        """Statuses seen while polling a transition are logged."""
        path = tmp_path / 'status.log'
        gate = Gate()
        window = make_window(tk_root, gate)
        window._open_log_file(str(path))
        window._go_btn.invoke()
        assert gate.entered.wait(TIMEOUT)
        window._poll_inner()
        gate.release.set()
        finish_transition(window)
        window._close_log_file()
        text = path.read_text(encoding='utf-8')
        assert ' | halfway\n' in text
        assert text.endswith(' | finished\n')

    def test_error_logged(self, tk_root, tmp_path):
        """A transition error is written to the log."""
        path = tmp_path / 'status.log'
        window = make_window(tk_root, Broken())
        window._open_log_file(str(path))
        window._on_go()
        finish_transition(window)
        window._close_log_file()
        assert 'ERROR during transition: boom' in path.read_text(
            encoding='utf-8')

    def test_browse_cancel(self, tk_root, monkeypatch):
        """Cancelling the log dialog opens no file."""
        patch_dialog(monkeypatch, 'asksaveasfilename', '')
        window = make_window(tk_root, Gate())
        window._on_browse_log()
        assert window._log_file_handle is None
        window._maybe_log_status('ignored')

    def test_unopenable_log(self, tk_root, tmp_path, caplog):
        """A log path that cannot be opened is logged and ignored."""
        window = make_window(tk_root, Gate())
        with caplog.at_level(logging.ERROR):
            window._open_log_file(str(tmp_path))
        assert window._log_file_handle is None
        assert 'Failed to open log file' in caplog.text


# ---------------------------------------------------------------
# Poll loop and closing
# ---------------------------------------------------------------

class TestPollAndClose:
    """Poll scheduling and window close."""

    @pytest.mark.parametrize('value, expected', [
        (200, 200),
        (10, 50),
        ('abc', 500),
    ])
    def test_poll_interval(self, tk_root, monkeypatch, value,
                           expected):
        """The poll interval is clamped and falls back to 500 ms."""
        window = make_window(tk_root, Gate())
        window.setvar(str(window._interval_var), value)
        after = ConstantFn()
        monkeypatch.setattr(window, 'after', after)
        window._schedule_next_poll()
        assert after.calls[0][0][0] == expected

    def test_poll_survives_errors(self, tk_root, monkeypatch, caplog):
        """An error during a poll is logged and polling continues."""
        window = make_window(tk_root, Gate())
        after = ConstantFn()
        monkeypatch.setattr(window, 'after', after)
        monkeypatch.setattr(window, '_poll_inner', raise_error)
        with caplog.at_level(logging.ERROR):
            window._poll()
        assert 'Error in poll loop' in caplog.text
        assert len(after.calls) == 1

    def test_close(self, tk_root, tmp_path):
        """Closing destroys the window, closes the log, calls back."""
        closed = ConstantFn()
        window = make_window(tk_root, Gate(), on_close_callback=closed)
        window._open_log_file(str(tmp_path / 'status.log'))
        window._on_close()
        assert closed.calls == [((), {})]
        assert window._log_file_handle is None
        assert not window.winfo_exists()

    @pytest.mark.xfail(strict=True, reason=(
        '_on_close does not cancel the poll timer, so a closed '
        'window keeps polling every interval forever'))
    def test_close_stops_polling(self, tk_root):
        """No poll callback is left pending after close."""
        window = make_window(tk_root, Gate())
        window._on_close()
        assert not [s for s in pending_after_scripts(tk_root)
                    if '_poll' in s]


class TestRunLauncher:
    """Tests for :meth:`StateRunnerWindow.run`."""

    def test_uses_existing_root(self, tk_root, monkeypatch):
        """With a root present, run() opens a Toplevel under it."""
        monkeypatch.setattr(tk, '_default_root', tk_root)
        monkeypatch.setattr(StateRunnerWindow, 'mainloop',
                            fake_mainloop)
        MAINLOOP_WINDOWS.clear()
        StateRunnerWindow.run(StateRunner(Gate()), title='T',
                              nickname='N', accent_color='Green')
        window = MAINLOOP_WINDOWS[-1]
        assert window.master is tk_root
        assert window.title() == 'N — T'
        window._on_close()
        assert tk_root.winfo_exists()

    def test_creates_root_and_kills_it(self, tk_root, monkeypatch):
        """Without a root, run() makes one; closing destroys it."""
        monkeypatch.setattr(tk, '_default_root', None)
        monkeypatch.setattr(StateRunnerWindow, 'mainloop',
                            fake_mainloop)
        MAINLOOP_WINDOWS.clear()
        StateRunnerWindow.run(StateRunner(Gate()), kill_on_close=True)
        window = MAINLOOP_WINDOWS[-1]
        new_root = window.master
        assert new_root is not tk_root
        window._on_close()
        with pytest.raises(tk.TclError):
            new_root.winfo_exists()


# ---------------------------------------------------------------
# State variables panel
# ---------------------------------------------------------------

def vars_window(tk_root):
    """Return (machine, window) for a Tunable machine."""
    machine = Tunable()
    return machine, make_window(tk_root, machine)


def vars_label(window):
    """Return the state-variables status label text."""
    return str(window._vars_status_label.cget('text'))


def form_values(window):
    """Return the raw values in the state-variables form."""
    return {name: var.get()
            for name, (_spec, var) in window._vars_widget_tree.items()}


def set_form(window, **values):
    """Set state-variable form fields by name."""
    for name, value in values.items():
        window._vars_widget_tree[name][1].set(value)


class TestStateVariablesPanel:
    """The State Variables panel edits the machine's variables."""

    def test_panel_shows_values(self, tk_root):
        """The panel exists and shows the machine's values."""
        _machine, window = vars_window(tk_root)
        assert window._has_state_vars
        assert form_values(window) == {'speed': '2', 'label': 'warm'}

    def test_apply(self, tk_root):
        """Apply validates the form and updates the machine."""
        machine, window = vars_window(tk_root)
        set_form(window, speed='7', label='hot')
        window._apply_vars_btn.invoke()
        assert machine.get_state_variables() == Knobs(
            speed=7, label='hot')
        assert vars_label(window) == 'Applied.'

    def test_apply_errors(self, tk_root):
        """Bad input or failed validation leaves the machine alone."""
        machine, window = vars_window(tk_root)
        set_form(window, speed='fast')
        window._on_apply_variables()
        assert vars_label(window).startswith('Input error: ')
        set_form(window, speed='')
        window._on_apply_variables()
        assert vars_label(window).startswith('Validation error: ')
        assert machine.get_state_variables() == Knobs(
            speed=2, label='warm')

    def test_external_change_refreshes(self, tk_root):
        """Setting variables on the machine refreshes the form."""
        machine, window = vars_window(tk_root)
        machine.set_state_variables(Knobs(speed=9, label='new'))
        tk_root.update()
        assert form_values(window) == {'speed': '9', 'label': 'new'}

    def test_refresh_button(self, tk_root):
        """Refresh discards form edits and reloads the values."""
        _machine, window = vars_window(tk_root)
        set_form(window, speed='100')
        window._refresh_vars_btn.invoke()
        assert form_values(window)['speed'] == '2'

    def test_disabled_while_busy(self, tk_root):
        """The form is disabled while busy, enabled when paused."""
        machine, window = vars_window(tk_root)
        entries = find_widgets(window._vars_fields_frame, ttk.Entry)
        window._go_btn.invoke()
        assert machine.entered.wait(TIMEOUT)
        assert state_of(window._apply_vars_btn) == 'disabled'
        assert {state_of(e) for e in entries} == {'disabled'}
        assert state_of(window._refresh_vars_btn) == 'normal'
        window._pause_btn.invoke()
        assert state_of(window._apply_vars_btn) == 'normal'
        assert {state_of(e) for e in entries} == {'normal'}
        window._resume_btn.invoke()
        machine.release.set()
        finish_transition(window)
        assert state_of(window._apply_vars_btn) == 'normal'

    def test_dump_and_load(self, tk_root, monkeypatch, tmp_path):
        """Dump writes the form to JSON and Load reads it back."""
        path = tmp_path / 'vars.json'
        _machine, window = vars_window(tk_root)
        set_form(window, speed='5')
        patch_dialog(monkeypatch, 'asksaveasfilename', str(path))
        window._on_dump_variables()
        assert vars_label(window) == f'Saved to {path}'
        assert Knobs.model_validate_json(path.read_text()) == Knobs(
            speed=5, label='warm')

        set_form(window, speed='0', label='x')
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        menu = find_widgets(window._vars_collapse._frame, tk.Menu)[0]
        menu.invoke(0)
        assert vars_label(window) == f'Loaded from {path}'
        assert form_values(window) == {'speed': '5', 'label': 'warm'}

    def test_dump_errors(self, tk_root, monkeypatch, tmp_path):
        """Dump reports bad input, validation and file errors."""
        save = patch_dialog(monkeypatch, 'asksaveasfilename', '')
        _machine, window = vars_window(tk_root)
        set_form(window, speed='fast')
        window._on_dump_variables()
        assert vars_label(window).startswith('Input error: ')
        set_form(window, speed='')
        window._on_dump_variables()
        assert vars_label(window).startswith('Validation error: ')
        assert not save.calls
        set_form(window, speed='1')
        window._on_dump_variables()
        assert len(save.calls) == 1
        bad = tmp_path / 'missing' / 'vars.json'
        patch_dialog(monkeypatch, 'asksaveasfilename', str(bad))
        window._on_dump_variables()
        assert vars_label(window).startswith('File error: ')

    def test_load_errors(self, tk_root, monkeypatch, tmp_path):
        """Load reports bad JSON; cancel changes nothing."""
        _machine, window = vars_window(tk_root)
        patch_dialog(monkeypatch, 'askopenfilename', '')
        window._on_load_variables()
        assert vars_label(window) == ''
        bad = tmp_path / 'bad.json'
        bad.write_text('{', encoding='utf-8')
        patch_dialog(monkeypatch, 'askopenfilename', str(bad))
        window._on_load_variables()
        assert vars_label(window).startswith('Load error: ')

    def test_load_invalid_still_fills(self, tk_root, monkeypatch,
                                      tmp_path):
        """Data failing validation is still loaded into the form."""
        path = tmp_path / 'vars.json'
        path.write_text('{"speed": "fast"}', encoding='utf-8')
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        _machine, window = vars_window(tk_root)
        window._on_load_variables()
        assert form_values(window)['speed'] == 'fast'

    @pytest.mark.xfail(strict=True, reason=(
        '_on_load_variables overwrites the validation warning with '
        '"Loaded from ..." so the warning is never seen'))
    def test_load_invalid_shows_warning(self, tk_root, monkeypatch,
                                        tmp_path):
        """Data failing validation leaves a visible warning."""
        path = tmp_path / 'vars.json'
        path.write_text('{"speed": "fast"}', encoding='utf-8')
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        _machine, window = vars_window(tk_root)
        window._on_load_variables()
        assert 'Validation warning' in vars_label(window)

    def test_close_removes_callback(self, tk_root):
        """Closing the window unregisters its change callback."""
        machine, window = vars_window(tk_root)
        assert '_state_runner_gui' in machine.state_vars._callbacks
        window._on_close()
        assert machine.state_vars._callbacks == {}

    def test_no_vars_methods_are_noops(self, tk_root, monkeypatch):
        """Without state variables the panel methods do nothing."""
        save = patch_dialog(monkeypatch, 'asksaveasfilename', 'x')
        load = patch_dialog(monkeypatch, 'askopenfilename', 'x')
        window = make_window(tk_root, Gate())
        window._refresh_state_variables()
        window._on_apply_variables()
        window._on_dump_variables()
        window._on_load_variables()
        window._set_variable_controls_enabled(False)
        assert not save.calls and not load.calls
