# State Runner for ox_ui.tkdantic

A tkinter GUI for driving `pytransitions` state machines with
support for long-running transitions, pause/resume, cancel, abort,
progress reporting, and status logging.

## Files

| File | Purpose |
|------|---------|
| `ox_ui/tkdantic/runnable.py` | `RunnableMachine` Protocol and `RunnableMachineHelper` base class |
| `ox_ui/tkdantic/state_runner.py` | `StateRunner` controller, `StateRunnerWindow` GUI, `DiagramWindow` placeholder |
| `tests/text/test_state_runner.py` | 24 pytest tests covering logic (no GUI tests) |

## Quick Start

```python
import time
from transitions import Machine
from ox_ui.tkdantic.runnable import RunnableMachineHelper
from ox_ui.tkdantic.state_runner import StateRunner, StateRunnerWindow


class MyPipeline(RunnableMachineHelper):
    STATES = ['idle', 'processing', 'done']
    TRANSITIONS = [
        {'trigger': 'start',  'source': 'idle',       'dest': 'processing'},
        {'trigger': 'finish', 'source': 'processing', 'dest': 'done'},
        {'trigger': 'reset',  'source': 'done',       'dest': 'idle'},
    ]

    def __init__(self):
        super().__init__()
        self._machine = Machine(
            model=self, states=self.STATES,
            transitions=self.TRANSITIONS, initial='idle',
        )

    def get_machine(self):
        return self._machine

    def on_enter_processing(self):
        """Long-running work goes in pytransitions callbacks."""
        for i in range(10):
            self.wait_if_paused()
            if self.is_cancelled():
                self.set_status('Cancelled.')
                return
            time.sleep(0.5)
            self.set_progress((i + 1) / 10)
            self.set_status(f'Step {i+1}/10')


# Standalone launch
pipeline = MyPipeline()
runner = StateRunner(pipeline)
StateRunnerWindow.run(runner, title='My Pipeline')
```

### Embedding with MainApp (builder.py)

```python
from ox_ui.tkdantic.command import Command
from ox_ui.tkdantic.builder import MainApp

pipeline = MyPipeline()
runner = StateRunner(pipeline)

commands = {
    'pipeline': Command(
        title='Pipeline Runner',
        parameters=[],
        description='Run the data pipeline',
        callback=lambda parent: StateRunnerWindow(parent, runner),
    ),
}

app = MainApp(title='My App', model_commands=commands)
app.mainloop()
```

## Architecture

### Separation of Concerns

The design separates three layers:

1. **Protocol (`RunnableMachine`)** — defines what the state
   machine must provide: access to the `transitions.Machine`,
   a model to call triggers on, pause/resume/cancel methods,
   and progress/status getters.

2. **Controller (`StateRunner`)** — pure logic, no tkinter
   dependency. Manages the worker thread, prevents concurrent
   transitions with a lock, captures exceptions. Fully testable
   without a display.

3. **GUI (`StateRunnerWindow`)** — tkinter Toplevel that polls
   the controller and updates widgets. Thin layer on top of
   `StateRunner`.

### Why Protocol (not ABC)

We use `typing.Protocol` with `@runtime_checkable` instead of
`abc.ABC` because ABC metaclass decorators have caused problems
in the past with multiple inheritance and metaclass conflicts.
Protocols achieve the same structural typing goal without
metaclass machinery.

### Leveraging pytransitions

A key design decision was to **not** define an `execute_transition`
method. Instead, all work happens through pytransitions' native
callback chain:

1. `prepare` (on transition)
2. `before` (on transition)
3. conditions checked
4. `before_state_change` (machine-level)
5. `on_exit_<old_state>` (on model)
6. **STATE CHANGES**
7. `on_enter_<new_state>` (on model)
8. `after_state_change` (machine-level)
9. `after` (on transition)

The `StateRunner` simply calls the trigger method on the model
(e.g., `model.start()`) and pytransitions orchestrates everything.
This means users get the full pytransitions callback lifecycle,
conditions, guards, and error semantics for free.

**Important pytransitions behaviour:**
- If a `before` callback raises, the state does NOT change.
- If `on_enter_<state>` raises, the state HAS already changed.
  The exception is still captured and reported.

### Threading Model

```
Worker thread                         GUI poll loop (every 500ms)
─────────────                         ──────────────────────────
calls model.trigger_name()            checks thread.is_alive()
  pytransitions runs callbacks          reads progress/status
  on_enter does work...                 updates widgets
  callbacks finish                      repeats...
trigger returns
thread ends                           sees is_alive() → False
                                        → refresh state display
                                        → refresh trigger dropdown
                                        → re-enable controls
```

**Rules:**
- The worker thread NEVER touches tkinter widgets.
- The GUI thread NEVER calls long-running code.
- Progress and status are exchanged through lock-guarded
  attributes that the GUI polls.
- A `threading.Lock` prevents concurrent transitions.

### Pause / Resume

Cooperative, using `threading.Event`:

- `pause()` clears the event (blocks `wait_if_paused()`).
- `resume()` sets the event (unblocks).
- The user calls `self.wait_if_paused()` inside their loops.
- Status automatically shows "Paused." while blocked.

### Cancel

Also cooperative:

- `cancel()` sets the cancel event AND resumes (so the thread
  unblocks if paused and can check `is_cancelled()`).
- The user checks `self.is_cancelled()` in their loops.
- After cancel, the machine stays in whatever state pytransitions
  put it in. The user is responsible for defining recovery
  triggers if needed.

### Abort

Emergency stop that kills the entire program via `os._exit(1)`.
A confirmation dialog is shown first. This is intentionally
the "big red button" — `os._exit` cannot be caught and
terminates all threads immediately.

### Exception Handling

If a pytransitions callback raises, the exception is:

1. Captured by the worker thread.
2. Stored in `runner.last_error`.
3. Logged via `logging.exception` (full stack trace to console).
4. Displayed in the status pane as "ERROR during transition: ...".
5. The GUI re-enables controls and shows whatever state the
   machine is now in.

## GUI Layout

1. **State display** — current state (bold) and all states list
   with current marked in brackets.
2. **Trigger dropdown + Go** — auto-populated from available
   triggers for the current state, auto-selects first trigger
   after each transition completes.
3. **Control buttons** — Pause, Resume, Cancel, Abort, Diagram.
   All with descriptive tooltips. Enabled/disabled based on
   whether a transition is running.
4. **Progress bar** — determinate (0–100%) when `get_progress()`
   returns a float, indeterminate when it returns None.
5. **Status pane** — replaces text on each poll (not append).
6. **Parameters** (collapsed) — update interval (default 500ms)
   and optional status log file with Browse button.

### Diagram Window

Placeholder `tk.Toplevel` showing a text summary of states
(with current marked) and transitions. Intended to be replaced
with a graphical diagram in the future.

### Status Log File

When a log file path is set via the Parameters panel:

- Status messages are appended with timestamps.
- Consecutive duplicate messages are deduplicated.
- Progress values are NOT logged.
- Format: `2025-03-17 14:23:05 | Processing step 3/10`

### Status Text Formatting

`format_status_text()` is a module-level hook that currently
returns text unchanged. It is designed to be extended later
to parse lightweight markup and apply tkinter Text widget tags
for bold, colour, etc.

## Testing

Run tests with:

```bash
python -m pytest tests/text/test_state_runner.py -v
```

24 tests cover:

- Protocol conformance
- State queries and trigger filtering
- Basic transitions (simple, round-trip, with work)
- Progress and status reporting
- Pause/resume (freezes progress, status message)
- Cancel (stops work early)
- Locking (prevents concurrent transitions, `is_busy()`)
- Exception handling (captured, state after error, clears on next)
- `reset_run_state` (clears cancel, progress, status)

All tests exercise `StateRunner` and `RunnableMachineHelper`
directly, without requiring a tkinter display.

## Dependencies

- `transitions` — pytransitions state machine library
- `tkinter` — standard library GUI toolkit
- `ox_ui.tkdantic.builder` — reuses `CollapsibleFrame` and
  `add_tooltip` for consistency with existing ox_ui GUI patterns
