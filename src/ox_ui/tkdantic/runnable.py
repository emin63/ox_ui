"""Protocol and helper for state machines driven by StateRunner.

This module defines the :class:`RunnableMachine` protocol that any
object must satisfy to be driven by :class:`StateRunner`, and a
convenience base class :class:`RunnableMachineHelper` that provides
default implementations of the threading utilities (pause, cancel,
progress, status).

Users subclass :class:`RunnableMachineHelper`, override
:meth:`get_machine`, and implement their long-running work inside
standard ``pytransitions`` callbacks (``on_enter_<state>``,
``before``, ``after``, etc.).

Example::

    from transitions import Machine
    from ox_ui.tkdantic.runnable import RunnableMachineHelper

    class MyPipeline(RunnableMachineHelper):
        def __init__(self):
            super().__init__()
            self._machine = Machine(
                model=self,
                states=['idle', 'running', 'done'],
                transitions=[
                    {'trigger': 'start', 'source': 'idle',
                     'dest': 'running'},
                ],
                initial='idle',
            )

        def get_machine(self):
            return self._machine

        def on_enter_running(self):
            for i in range(10):
                self.wait_if_paused()
                if self.is_cancelled():
                    self.set_status('Cancelled.')
                    return
                self.set_progress((i + 1) / 10)
                self.set_status(f'Step {i + 1}/10')
"""

import logging
import threading
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel
from transitions import Machine

from ox_ui.tkdantic.callbacks import SimpleCallback
from ox_ui.tkdantic.run_controller import RunController
from ox_ui.tkdantic.state_variables import StateVariables
from ox_ui.tkdantic.status_manager import Status

LOGGER = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Protocol
# -------------------------------------------------------------------

@runtime_checkable
class RunnableMachine(Protocol):
    """Protocol for objects driven by :class:`StateRunner`.

    Implementors must provide access to a ``pytransitions``
    :class:`Machine`, a model object on which triggers are called,
    cooperative pause/resume/cancel methods, and progress/status
    reporting.
    """

    def get_machine(self) -> Machine:
        """Return the ``pytransitions`` Machine instance."""
        ...

    def get_model(self) -> Any:
        """Return the model object that owns trigger methods.

        In most cases this is ``self`` (the default in
        :class:`RunnableMachineHelper`).
        """
        ...

    def pause(self) -> None:
        """Request the current transition to pause."""
        ...

    def resume(self) -> None:
        """Resume a paused transition."""
        ...

    def cancel(self) -> None:
        """Request cooperative cancellation."""
        ...

    def get_progress(self) -> Optional[float]:
        """Return progress as a float in [0, 1] or None."""
        ...

    def get_status_text(self) -> str:
        """Return current human-readable status string."""
        ...

    def get_state_variables(self) -> Optional[BaseModel]:
        """Return a snapshot of the current state variables.

        Returns a pydantic model instance representing the
        current configurable state, or ``None`` if this machine
        has no user-configurable variables.

        The returned instance is a **snapshot**; callers should
        not mutate it.
        """
        ...

    def set_state_variables(self, variables: BaseModel) -> None:
        """Replace the current state variables wholesale.

        Callers should only invoke this when the machine is
        idle or paused.

        :param variables: validated pydantic model instance.
        """
        ...


# -------------------------------------------------------------------
# Helper base class
# -------------------------------------------------------------------

class RunnableMachineHelper:
    """Convenience base class satisfying :class:`RunnableMachine`.

    Provides thread-safe pause, resume, cancel, progress, and
    status utilities.  Subclasses must override :meth:`get_machine`.

    The threading helpers are designed to be called from within
    ``pytransitions`` callbacks (``on_enter_<state>``, ``before``,
    ``after``, etc.) which run on the worker thread spawned by
    :class:`StateRunner`.

    Three public attributes expose the cooperative primitives so
    they can be handed to helper functions or other objects
    without exposing the full helper:

    * :attr:`status` — a :class:`Status` instance owning the
      human-readable status text and named headers.
    * :attr:`controller` — a :class:`RunController` instance
      owning the pause / cancel events.
    * :attr:`state_vars` — a :class:`StateVariables` instance
      owning the user-configurable pydantic model and the
      callbacks that fire when it is replaced.

    The status-related, control-related, and state-variable
    methods on this class (:meth:`set_status`, :meth:`pause`,
    :meth:`wait_if_paused`, :meth:`get_state_variables`, etc.)
    are thin delegates to those attributes, kept for backward
    compatibility and to satisfy the :class:`RunnableMachine`
    protocol.
    """

    def __init__(
        self,
        status: Optional[Status] = None,
        controller: Optional[RunController] = None,
        state_vars: Optional[StateVariables] = None,
    ) -> None:
        """Initialise threading primitives and state.

        :param status: optional :class:`Status` instance to use
            for the human-readable status text.  If ``None``
            (the default), a fresh :class:`Status` is created.
            Pass an existing instance to share a status display
            between cooperating objects.
        :param controller: optional :class:`RunController`
            instance owning the pause / cancel events.  If
            ``None`` (the default), a fresh :class:`RunController`
            is created.  Pass an existing instance to share
            cooperative control between cooperating objects.
        :param state_vars: optional :class:`StateVariables`
            instance owning the user-configurable pydantic model
            and its changed-callbacks.  If ``None`` (the default),
            a fresh :class:`StateVariables` is created.  Pass an
            existing instance to share state variables between
            cooperating objects.
        """
        # Cooperative pause / cancel.  Owns its own events.
        self.controller: RunController = (
            controller if controller is not None
            else RunController()
        )

        # Guards reads/writes of _progress.
        self._state_lock = threading.Lock()
        self._progress: Optional[float] = None

        # Human-readable status (raw text + named headers).
        # Owns its own lock internally.
        self.status: Status = (
            status if status is not None else Status()
        )

        # User-configurable pydantic model + changed-callbacks.
        # Owns its own lock internally.
        self.state_vars: StateVariables = (
            state_vars if state_vars is not None
            else StateVariables()
        )

    # --- abstract (must override) --------------------------------

    def get_machine(self) -> Machine:
        """Return the pytransitions Machine.

        Subclasses **must** override this.
        """
        raise NotImplementedError(
            'Subclasses must implement get_machine().'
        )

    # --- defaults ------------------------------------------------

    def get_model(self) -> Any:
        """Return the model that owns trigger methods.

        Default returns ``self``, which is correct when the
        Machine was created with ``model=self``.
        """
        return self

    # --- pause / resume / cancel ---------------------------------

    def pause(self) -> None:
        """Request the worker to pause at the next safe point.

        Delegates to :attr:`controller`.
        """
        self.controller.pause()

    def resume(self) -> None:
        """Resume a paused worker.

        Delegates to :attr:`controller`.
        """
        self.controller.resume()

    def cancel(self) -> None:
        """Request cooperative cancellation.

        Delegates to :attr:`controller`.
        """
        self.controller.cancel()

    # --- progress / status (thread-safe) -------------------------

    def get_progress(self) -> Optional[float]:
        """Return progress in [0, 1] or None if indeterminate."""
        with self._state_lock:
            return self._progress

    def get_status_text(self) -> str:
        """Return headers (if any) followed by the current status.

        Delegates to :attr:`status`.
        """
        return self.status.get_status_text()

    def get_raw_status(self) -> str:
        """Return just the raw status text, without headers.

        Delegates to :attr:`status`.
        """
        return self.status.get_raw_status()

    def set_progress(self, value: Optional[float]) -> None:
        """Set progress.  Call from pytransitions callbacks.

        :param value: float in [0, 1] or None for indeterminate.
        """
        with self._state_lock:
            self._progress = value

    def set_status(self, text: str) -> None:
        """Set the raw status text.  Call from pytransitions callbacks.

        Delegates to :attr:`status`.  This replaces only the raw
        portion; status headers are unaffected.

        :param text: human-readable status message.
        """
        self.status.set_status(text)

    def append_status(self, text: str) -> None:
        """Append to the raw status text.

        Delegates to :attr:`status`.

        :param text: text to append (a newline is prepended
            automatically if the raw status is non-empty).
        """
        self.status.append_status(text)

    def add_status_header(self, name: str, text: str) -> None:
        """Add or replace a named status header.

        Delegates to :attr:`status`.

        :param name: unique key for this header.
        :param text: header text to display.
        """
        self.status.add_status_header(name, text)

    def remove_status_header(self, name: str) -> None:
        """Remove a status header by name.

        Delegates to :attr:`status`.  Silently ignores unknown
        names.

        :param name: key passed to :meth:`add_status_header`.
        """
        self.status.remove_status_header(name)

    # --- worker-thread helpers -----------------------------------

    def wait_if_paused(self) -> None:
        """Block until resumed.  Call inside long-running loops.

        Delegates to :attr:`controller`, passing :attr:`status`
        so the raw status text is set to ``'Paused.'`` for the
        duration of the pause and restored on resume.  Status
        headers are not affected.
        """
        self.controller.wait_if_paused(self.status)

    def is_cancelled(self) -> bool:
        """Return True if cancellation has been requested.

        Delegates to :attr:`controller`.
        """
        return self.controller.is_cancelled()

    def reset_run_state(self) -> None:
        """Reset pause/cancel/progress/status for a new transition.

        Called by :class:`StateRunner` before spawning the
        worker thread.
        """
        self.controller.reset()
        with self._state_lock:
            self._progress = None
        self.status.clear()

    # --- state variables -----------------------------------------

    def get_state_variables(self) -> Optional[BaseModel]:
        """Return a deep copy of the current state variables.

        Delegates to :attr:`state_vars`.  Returns ``None`` if
        this machine has no user-configurable variables (the
        default).  The returned instance is a **snapshot**;
        callers should not mutate it.
        """
        return self.state_vars.get()

    def set_state_variables(
        self, variables: BaseModel,
    ) -> None:
        """Replace the current state variables wholesale.

        Delegates to :attr:`state_vars`.  A deep copy of
        *variables* is stored so the caller retains no shared
        reference.  After storing, all registered state-changed
        callbacks are executed.

        Callers should only invoke this when the machine is
        idle or paused.

        :param variables: validated pydantic model instance.
        """
        self.state_vars.set(variables)

    # --- state-changed callbacks ---------------------------------

    def add_state_changed_callback(
        self, name: str, callback: SimpleCallback,
    ) -> None:
        """Register a named callback for state-variable changes.

        Delegates to :attr:`state_vars`.  If *name* already
        exists it is replaced.

        :param name: unique key for this callback.
        :param callback: :class:`SimpleCallback` to invoke.
        """
        self.state_vars.add_changed_callback(name, callback)

    def remove_state_changed_callback(self, name: str) -> None:
        """Remove a previously registered callback by name.

        Delegates to :attr:`state_vars`.  Silently ignores
        unknown names.

        :param name: the key passed to
            :meth:`add_state_changed_callback`.
        """
        self.state_vars.remove_changed_callback(name)

    def execute_state_changed_callbacks(self) -> None:
        """Run all registered state-changed callbacks.

        Delegates to :attr:`state_vars`.  The callback dict is
        snapshotted under the lock and then released **before**
        any callback executes.  This prevents deadlocks when a
        callback calls :meth:`get_state_variables` or registers
        / removes other callbacks.

        Exceptions in individual callbacks are logged and
        do not prevent remaining callbacks from running.
        """
        self.state_vars.execute_changed_callbacks()
