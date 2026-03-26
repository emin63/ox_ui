"""Run a pytransitions state machine with a tkinter GUI.

This module provides:

- :class:`StateRunner` — a logic controller that drives a
  :class:`~ox_ui.tkdantic.runnable.RunnableMachine` in a worker
  thread, preventing concurrent transitions and capturing errors.

- :class:`StateRunnerWindow` — a tkinter ``Toplevel`` that
  displays the current state, lets the user pick and fire
  triggers, and shows progress and status text.

- :class:`DiagramWindow` — a simple text-based placeholder for
  a future graphical state diagram.

- :func:`format_status_text` — a hook for future rich-text
  formatting of status messages.

Usage (standalone)::

    pipeline = MyPipeline()  # satisfies RunnableMachine protocol
    runner = StateRunner(pipeline)
    StateRunnerWindow.run(runner, title='My Pipeline')

Usage (embedded with existing Tk root)::

    root = tk.Tk()
    runner = StateRunner(pipeline)
    StateRunnerWindow(root, runner)
    root.mainloop()
"""

import datetime
import logging
import os
import threading
from tkinter import filedialog, messagebox
from typing import Optional

import tkinter as tk
from tkinter import ttk, scrolledtext

from ox_ui.tkdantic.builder import (
    CollapsibleFrame,
    add_tooltip,
)
from ox_ui.tkdantic.runnable import RunnableMachine

LOGGER = logging.getLogger(__name__)

_DEFAULT_UPDATE_MS = 500


# -------------------------------------------------------------------
# Status text formatting hook
# -------------------------------------------------------------------

def format_status_text(text: str) -> str:
    """Format status text for display in the status pane.

    Currently returns *text* unchanged.  This function is a hook
    for future enhancement: it could parse lightweight markup
    (e.g., lines starting with ``#`` rendered bold) and return
    text with tkinter ``Text``-widget tag information for bold,
    colour, etc.

    :param text: raw status string from the state machine.
    :returns: formatted string (currently identical to *text*).
    """
    return text


# -------------------------------------------------------------------
# StateRunner — logic controller (no tkinter dependency)
# -------------------------------------------------------------------

class StateRunner:
    """Drive a :class:`RunnableMachine` from a worker thread.

    Responsibilities:

    * Spawn one worker thread per transition and prevent
      concurrent transitions via a lock.
    * Capture exceptions raised during transitions.
    * Expose ``is_busy``, ``last_error``, and delegation
      methods for pause / resume / cancel.

    This class has **no** tkinter dependency and can be tested
    without a display.
    """

    def __init__(self, runnable: RunnableMachine) -> None:
        """Create a runner for *runnable*.

        :param runnable: object satisfying RunnableMachine.
        :raises TypeError: if *runnable* does not satisfy the
            protocol.
        """
        if not isinstance(runnable, RunnableMachine):
            raise TypeError(
                f'{type(runnable).__name__} does not satisfy '
                f'the RunnableMachine protocol.'
            )
        self.runnable = runnable
        self._transition_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self.last_error: Optional[Exception] = None

    # --- state queries ------------------------------------------

    def get_current_state(self) -> str:
        """Return the current state name from the machine."""
        machine = self.runnable.get_machine()
        model = self.runnable.get_model()
        attr = machine.model_attribute
        return str(getattr(model, attr))

    def get_all_states(self) -> list:
        """Return a list of all state names."""
        machine = self.runnable.get_machine()
        return [s.name for s in machine.states.values()]

    def get_available_triggers(self) -> list:
        """Return user-defined triggers for the current state.

        Filters out auto-transitions (``to_<state>``) which
        pytransitions generates by default.
        """
        machine = self.runnable.get_machine()
        current = self.get_current_state()
        all_triggers = machine.get_triggers(current)
        auto_prefixes = tuple(
            f'to_{s}' for s in self.get_all_states()
        )
        return [
            t for t in all_triggers
            if t not in auto_prefixes
        ]

    # --- transition control -------------------------------------

    def is_busy(self) -> bool:
        """Return True if a transition is currently running."""
        return (
            self._worker_thread is not None
            and self._worker_thread.is_alive()
        )

    def start_transition(self, trigger_name: str) -> bool:
        """Fire *trigger_name* in a worker thread.

        Returns True if the transition was started, False if
        the runner is already busy.
        """
        if not self._transition_lock.acquire(blocking=False):
            LOGGER.warning(
                'Transition already in progress; ignoring %s.',
                trigger_name,
            )
            return False

        self.last_error = None
        self.runnable.reset_run_state()

        self._worker_thread = threading.Thread(
            target=self._run_trigger,
            args=(trigger_name,),
            daemon=True,
            name=f'StateRunner-{trigger_name}',
        )
        self._worker_thread.start()
        LOGGER.info('Started transition: %s', trigger_name)
        return True

    def _run_trigger(self, trigger_name: str) -> None:
        """Execute the trigger on the model (runs in worker).

        Captures any exception so the GUI poll loop can report
        it, and always releases the transition lock.
        """
        try:
            model = self.runnable.get_model()
            trigger_method = getattr(model, trigger_name)
            trigger_method()
        except Exception as exc:
            self.last_error = exc
            LOGGER.exception(
                'Exception during transition %s: %s',
                trigger_name, exc,
            )
        finally:
            self._transition_lock.release()

    # --- delegation to runnable ---------------------------------

    def request_pause(self) -> None:
        """Delegate pause to the runnable."""
        self.runnable.pause()

    def request_resume(self) -> None:
        """Delegate resume to the runnable."""
        self.runnable.resume()

    def request_cancel(self) -> None:
        """Delegate cancel to the runnable."""
        self.runnable.cancel()


# -------------------------------------------------------------------
# DiagramWindow — placeholder for future graphical diagram
# -------------------------------------------------------------------

class DiagramWindow(tk.Toplevel):
    """Simple text display of states and transitions.

    This is a **placeholder** for a future graphical state
    diagram.  Currently it shows a text summary listing all
    states (with the current state highlighted) and all
    user-defined transitions.
    """

    def __init__(self, parent: tk.Misc, runner: StateRunner):
        """Create the diagram window.

        :param parent: parent tkinter widget.
        :param runner: the StateRunner to inspect.
        """
        super().__init__(parent)
        self.title('State Diagram')
        self.minsize(400, 300)
        self._runner = runner
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        """Create the scrolled text widget."""
        self._text = scrolledtext.ScrolledText(
            self, wrap='word', state='disabled',
            font=('Consolas', 10),
        )
        self._text.pack(fill='both', expand=True, padx=8,
                        pady=8)

    def _populate(self) -> None:
        """Fill the text widget with state/transition info."""
        lines = self._build_diagram_text()
        self._text.config(state='normal')
        self._text.delete('1.0', 'end')
        self._text.insert('end', '\n'.join(lines))
        self._text.config(state='disabled')

    def _build_diagram_text(self) -> list:
        """Build a list of text lines for the diagram.

        Lists all states with the current state marked, then
        lists every user-defined transition.
        """
        runner = self._runner
        current = runner.get_current_state()
        all_states = runner.get_all_states()

        lines = ['States:', '']
        for s in all_states:
            marker = '  -->  ' if s == current else '       '
            lines.append(f'{marker}{s}')

        lines.append('')
        lines.append('Transitions:')
        lines.append('')

        machine = runner.runnable.get_machine()
        auto_names = {f'to_{s}' for s in all_states}
        for name, event in machine.events.items():
            if name in auto_names:
                continue
            for src, trans_list in event.transitions.items():
                for t in trans_list:
                    lines.append(
                        f'  {t.source} --{name}--> {t.dest}'
                    )

        return lines


# -------------------------------------------------------------------
# StateRunnerWindow — tkinter GUI
# -------------------------------------------------------------------

class StateRunnerWindow(tk.Toplevel):
    """Tkinter GUI for driving a :class:`StateRunner`.

    Layout (top to bottom):

    1. State display — current state and all states.
    2. Trigger dropdown + Go button.
    3. Control buttons — Pause, Resume, Cancel, Abort, Diagram.
    4. Progress bar.
    5. Status text pane (replaces on each update).
    6. Collapsible parameters (update interval, log file).
    """

    def __init__(
        self,
        parent: tk.Misc,
        runner: StateRunner,
        title: str = 'State Runner',
    ):
        """Create the state runner GUI.

        :param parent: parent tkinter widget.
        :param runner: the StateRunner to drive.
        :param title: window title string.
        """
        super().__init__(parent)
        self._runner = runner
        self.title(title)
        self.minsize(560, 480)

        self._was_busy = False
        self._last_logged_status: Optional[str] = None
        self._log_file_handle = None

        self._build_state_frame()
        self._build_trigger_frame()
        self._build_button_frame()
        self._build_progress_bar()
        self._build_status_pane()
        self._build_parameters_frame()

        self._refresh_state_display()
        self._refresh_trigger_dropdown()
        self._set_controls_idle()

        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._poll()

    # =============================================================
    # UI construction
    # =============================================================

    def _build_state_frame(self) -> None:
        """Create the state display area."""
        frame = ttk.LabelFrame(self, text='State', padding=6)
        frame.pack(fill='x', padx=8, pady=(8, 4))

        self._state_label = ttk.Label(
            frame, text='', font=('TkDefaultFont', 11, 'bold'),
        )
        self._state_label.pack(anchor='w')

        self._all_states_label = ttk.Label(
            frame, text='', foreground='gray',
        )
        self._all_states_label.pack(anchor='w', pady=(2, 0))

    def _build_trigger_frame(self) -> None:
        """Create the trigger dropdown and Go button."""
        frame = ttk.Frame(self, padding=(8, 4))
        frame.pack(fill='x')

        ttk.Label(frame, text='Trigger:').pack(side='left')

        self._trigger_var = tk.StringVar()
        self._trigger_menu = ttk.OptionMenu(
            frame, self._trigger_var, '',
        )
        self._trigger_menu.pack(
            side='left', fill='x', expand=True, padx=(4, 4),
        )

        self._go_btn = ttk.Button(
            frame, text='Go', command=self._on_go,
        )
        self._go_btn.pack(side='left')
        add_tooltip(self._go_btn, 'Fire the selected trigger.')

    def _make_button(
        self, parent, text, command, tooltip, **pack_kw,
    ) -> ttk.Button:
        """Create a button with a tooltip and pack it.

        :param parent: parent frame.
        :param text: button label.
        :param command: callback.
        :param tooltip: tooltip text.
        :param pack_kw: extra keyword args for ``pack()``.
        :returns: the new button widget.
        """
        btn = ttk.Button(parent, text=text, command=command)
        btn.pack(**pack_kw)
        add_tooltip(btn, tooltip)
        return btn

    def _build_button_frame(self) -> None:
        """Create Pause, Resume, Cancel, Abort, Diagram."""
        frame = ttk.Frame(self, padding=(8, 4))
        frame.pack(fill='x')

        self._pause_btn = self._make_button(
            frame, 'Pause', self._on_pause,
            'Pause the current transition. Work will '
            'stop at the next safe point.',
            side='left',
        )
        self._resume_btn = self._make_button(
            frame, 'Resume', self._on_resume,
            'Resume a paused transition.',
            side='left', padx=(4, 0),
        )
        self._cancel_btn = self._make_button(
            frame, 'Cancel', self._on_cancel,
            'Cooperatively cancel the current transition. '
            'The machine will stop at the next safe point.',
            side='left', padx=(4, 0),
        )
        self._abort_btn = self._make_button(
            frame, 'Abort', self._on_abort,
            'EMERGENCY STOP. Kills the entire program '
            'immediately. All unsaved work will be lost. '
            'Use Cancel for a clean stop.',
            side='left', padx=(4, 0),
        )
        self._diagram_btn = self._make_button(
            frame, 'Diagram', self._on_diagram,
            'Show a text summary of states and '
            'transitions.',
            side='right',
        )

    def _build_progress_bar(self) -> None:
        """Create the progress bar."""
        self._progress_bar = ttk.Progressbar(
            self, mode='indeterminate', length=300,
        )
        self._progress_bar.pack(
            fill='x', padx=8, pady=(4, 4),
        )

    def _build_status_pane(self) -> None:
        """Create the status text display."""
        frame = ttk.LabelFrame(self, text='Status', padding=6)
        frame.pack(
            fill='both', expand=True, padx=8, pady=(4, 4),
        )

        self._status_text = scrolledtext.ScrolledText(
            frame, height=6, wrap='word', state='disabled',
            font=('Consolas', 10),
        )
        self._status_text.pack(fill='both', expand=True)

    def _build_parameters_frame(self) -> None:
        """Create the collapsible parameters area."""
        self._params_frame = CollapsibleFrame(
            self, text='Parameters', collapsed=True,
            padding=6,
        )
        self._params_frame.pack(fill='x', padx=8, pady=(4, 8))
        inner = self._params_frame.inner

        self._build_interval_param(inner, row=0)
        self._build_logfile_param(inner, row=1)
        inner.columnconfigure(1, weight=1)

    def _build_interval_param(
        self, inner: ttk.Frame, row: int,
    ) -> None:
        """Add the update-interval entry to *inner*."""
        ttk.Label(inner, text='Update interval (ms):').grid(
            row=row, column=0, sticky='w', padx=(0, 4),
        )
        self._interval_var = tk.IntVar(value=_DEFAULT_UPDATE_MS)
        entry = ttk.Entry(
            inner, textvariable=self._interval_var, width=8,
        )
        entry.grid(row=row, column=1, sticky='w')
        add_tooltip(
            entry,
            'How often (ms) the GUI polls for status '
            'and progress updates.',
        )

    def _build_logfile_param(
        self, inner: ttk.Frame, row: int,
    ) -> None:
        """Add the log-file entry and browse button."""
        ttk.Label(inner, text='Status log file:').grid(
            row=row, column=0, sticky='w', padx=(0, 4),
            pady=(4, 0),
        )
        log_frame = ttk.Frame(inner)
        log_frame.grid(
            row=row, column=1, sticky='ew', pady=(4, 0),
        )

        self._log_path_var = tk.StringVar(value='')
        log_entry = ttk.Entry(
            log_frame, textvariable=self._log_path_var,
            width=30,
        )
        log_entry.pack(side='left', fill='x', expand=True)
        add_tooltip(
            log_entry,
            'Path to a file where status messages will '
            'be logged.  Leave empty to disable.',
        )

        browse = ttk.Button(
            log_frame, text='Browse...',
            command=self._on_browse_log,
        )
        browse.pack(side='left', padx=(4, 0))

    # =============================================================
    # UI update helpers
    # =============================================================

    def _refresh_state_display(self) -> None:
        """Update the state label and all-states list."""
        current = self._runner.get_current_state()
        all_states = self._runner.get_all_states()

        self._state_label.config(text=f'Current: {current}')

        parts = []
        for s in all_states:
            if s == current:
                parts.append(f'[{s}]')
            else:
                parts.append(s)
        self._all_states_label.config(
            text='  '.join(parts),
        )

    def _refresh_trigger_dropdown(self) -> None:
        """Repopulate the trigger dropdown for current state."""
        triggers = self._runner.get_available_triggers()
        menu = self._trigger_menu['menu']
        menu.delete(0, 'end')

        if triggers:
            for t in triggers:
                menu.add_command(
                    label=t,
                    command=lambda v=t: self._trigger_var.set(v),
                )
            self._trigger_var.set(triggers[0])
        else:
            self._trigger_var.set('')

    def _update_status_pane(self, text: str) -> None:
        """Replace the status pane contents with *text*."""
        self._status_text.config(state='normal')
        self._status_text.delete('1.0', 'end')
        self._status_text.insert('end', text)
        self._status_text.config(state='disabled')

    def _update_progress_bar(
        self, value: Optional[float],
    ) -> None:
        """Update the progress bar from a 0-1 float or None."""
        if value is None:
            if str(self._progress_bar.cget('mode')) != (
                'indeterminate'
            ):
                self._progress_bar.config(mode='indeterminate')
                self._progress_bar.start(50)
        else:
            if str(self._progress_bar.cget('mode')) != (
                'determinate'
            ):
                self._progress_bar.stop()
                self._progress_bar.config(mode='determinate')
            self._progress_bar.config(
                value=int(value * 100),
            )

    def _stop_progress_bar(self) -> None:
        """Stop and reset the progress bar."""
        self._progress_bar.stop()
        self._progress_bar.config(
            mode='determinate', value=0,
        )

    # =============================================================
    # Control-state management
    # =============================================================

    def _set_controls_busy(self) -> None:
        """Disable trigger controls; enable pause/cancel."""
        self._go_btn.config(state='disabled')
        self._trigger_menu.config(state='disabled')
        self._pause_btn.config(state='normal')
        self._resume_btn.config(state='disabled')
        self._cancel_btn.config(state='normal')

    def _set_controls_paused(self) -> None:
        """Switch pause to disabled, resume to enabled."""
        self._pause_btn.config(state='disabled')
        self._resume_btn.config(state='normal')

    def _set_controls_idle(self) -> None:
        """Enable trigger controls; disable pause/resume."""
        has_triggers = bool(self._trigger_var.get())
        go_state = 'normal' if has_triggers else 'disabled'
        self._go_btn.config(state=go_state)
        self._trigger_menu.config(state='normal')
        self._pause_btn.config(state='disabled')
        self._resume_btn.config(state='disabled')
        self._cancel_btn.config(state='disabled')

    # =============================================================
    # Button callbacks
    # =============================================================

    def _on_go(self) -> None:
        """Fire the selected trigger."""
        trigger = self._trigger_var.get()
        if not trigger:
            return
        started = self._runner.start_transition(trigger)
        if started:
            self._was_busy = True
            self._set_controls_busy()
            self._update_status_pane(f'Running: {trigger}')

    def _on_pause(self) -> None:
        """Request a pause."""
        self._runner.request_pause()
        self._set_controls_paused()

    def _on_resume(self) -> None:
        """Request a resume."""
        self._runner.request_resume()
        self._set_controls_busy()

    def _on_cancel(self) -> None:
        """Request cooperative cancellation."""
        self._update_status_pane(
            'Cancel requested, waiting for safe stop...',
        )
        self._cancel_btn.config(state='disabled')
        self._runner.request_cancel()

    def _on_abort(self) -> None:
        """Kill the entire program after confirmation."""
        confirmed = messagebox.askyesno(
            'Abort Program',
            'This will immediately kill the ENTIRE '
            'program.\n\n'
            'All unsaved work will be lost.\n\n'
            'Continue?',
            icon='warning',
            parent=self,
        )
        if confirmed:
            LOGGER.critical('User initiated abort.')
            self._close_log_file()
            # os._exit bypasses all cleanup and cannot be
            # caught, which is the desired behaviour for an
            # emergency stop.
            os._exit(1)

    def _on_diagram(self) -> None:
        """Open the diagram placeholder window."""
        DiagramWindow(self, self._runner)

    def _on_browse_log(self) -> None:
        """Let the user choose a status log file."""
        path = filedialog.asksaveasfilename(
            parent=self,
            title='Choose status log file',
            defaultextension='.log',
            filetypes=[
                ('Log files', '*.log'),
                ('Text files', '*.txt'),
                ('All files', '*.*'),
            ],
        )
        if path:
            self._log_path_var.set(path)
            self._open_log_file(path)

    # =============================================================
    # Status logging
    # =============================================================

    def _open_log_file(self, path: str) -> None:
        """Open (or reopen) the status log file for appending."""
        self._close_log_file()
        try:
            self._log_file_handle = open(
                path, 'a', encoding='utf-8',
            )
            LOGGER.info('Opened status log: %s', path)
        except OSError:
            LOGGER.exception(
                'Failed to open log file: %s', path,
            )
            self._log_file_handle = None

    def _close_log_file(self) -> None:
        """Close the log file if open."""
        if self._log_file_handle is not None:
            try:
                self._log_file_handle.close()
            except OSError:
                LOGGER.exception('Error closing log file.')
            self._log_file_handle = None

    def _maybe_log_status(self, status: str) -> None:
        """Append *status* to log file if it changed.

        Skips writing if the status is identical to the most
        recently logged message, to avoid flooding the log.
        Progress information is not logged.
        """
        if self._log_file_handle is None:
            return
        if status == self._last_logged_status:
            return
        self._last_logged_status = status
        timestamp = datetime.datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S',
        )
        try:
            self._log_file_handle.write(
                f'{timestamp} | {status}\n',
            )
            self._log_file_handle.flush()
        except OSError:
            LOGGER.exception('Error writing to log file.')

    # =============================================================
    # Poll loop
    # =============================================================

    def _poll(self) -> None:
        """Periodically update the GUI from runner state.

        Scheduled via ``after()`` so all tkinter calls happen
        on the main thread.
        """
        try:
            self._poll_inner()
        except Exception:
            LOGGER.exception('Error in poll loop.')
        self._schedule_next_poll()

    def _poll_inner(self) -> None:
        """Core polling logic, separated for clarity."""
        if self._runner.is_busy():
            self._poll_while_busy()
        elif self._was_busy:
            self._poll_transition_finished()

    def _poll_while_busy(self) -> None:
        """Update progress and status during a transition."""
        progress = self._runner.runnable.get_progress()
        self._update_progress_bar(progress)

        raw = self._runner.runnable.get_status_text()
        self._update_status_pane(format_status_text(raw))
        self._maybe_log_status(raw)

    def _poll_transition_finished(self) -> None:
        """Handle the moment a transition completes."""
        self._was_busy = False
        self._stop_progress_bar()

        error = self._runner.last_error
        if error is not None:
            msg = f'ERROR during transition: {error}'
            self._update_status_pane(
                format_status_text(msg),
            )
            self._maybe_log_status(msg)
        else:
            raw = self._runner.runnable.get_status_text()
            if raw:
                self._update_status_pane(
                    format_status_text(raw),
                )
                self._maybe_log_status(raw)

        self._refresh_state_display()
        self._refresh_trigger_dropdown()
        self._set_controls_idle()

    def _schedule_next_poll(self) -> None:
        """Schedule the next poll with the configured interval."""
        try:
            interval = self._interval_var.get()
            if interval < 50:
                interval = 50
        except (tk.TclError, ValueError):
            interval = _DEFAULT_UPDATE_MS
        self.after(interval, self._poll)

    # =============================================================
    # Window lifecycle
    # =============================================================

    def _on_close(self) -> None:
        """Handle window close: clean up resources."""
        self._close_log_file()
        self.destroy()

    # =============================================================
    # Convenience launcher
    # =============================================================

    @classmethod
    def run(
        cls,
        runner: StateRunner,
        title: str = 'State Runner',
    ) -> None:
        """Launch a standalone window and enter mainloop.

        If a Tk root already exists, this creates a Toplevel
        under it.  Otherwise it creates a new Tk root first.

        :param runner: the :class:`StateRunner` to display.
        :param title: window title string.
        """
        root = tk._default_root  # noqa: SLF001
        if root is None:
            root = tk.Tk()
            root.withdraw()
        window = cls(root, runner, title=title)
        window.mainloop()
