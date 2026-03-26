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

from transitions import Machine

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
    """

    def __init__(self) -> None:
        """Initialise threading primitives and state."""
        # Set means "running" (not paused).
        self._pause_event = threading.Event()
        self._pause_event.set()

        # Set means "cancel requested".
        self._cancel_event = threading.Event()

        # Guards reads/writes of _progress and _status_text.
        self._state_lock = threading.Lock()
        self._progress: Optional[float] = None
        self._status_text: str = ''
        self._status_before_pause: str = ''

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
        """Request the worker to pause at the next safe point."""
        self._pause_event.clear()
        LOGGER.debug('Pause requested.')

    def resume(self) -> None:
        """Resume a paused worker."""
        self._pause_event.set()
        LOGGER.debug('Resume requested.')

    def cancel(self) -> None:
        """Request cooperative cancellation."""
        # Also resume in case we are paused, so the worker
        # thread unblocks and can check is_cancelled().
        self._cancel_event.set()
        self._pause_event.set()
        LOGGER.debug('Cancel requested.')

    # --- progress / status (thread-safe) -------------------------

    def get_progress(self) -> Optional[float]:
        """Return progress in [0, 1] or None if indeterminate."""
        with self._state_lock:
            return self._progress

    def get_status_text(self) -> str:
        """Return the current status string."""
        with self._state_lock:
            return self._status_text

    def set_progress(self, value: Optional[float]) -> None:
        """Set progress.  Call from pytransitions callbacks.

        :param value: float in [0, 1] or None for indeterminate.
        """
        with self._state_lock:
            self._progress = value

    def set_status(self, text: str) -> None:
        """Set status text.  Call from pytransitions callbacks.

        :param text: human-readable status message.
        """
        with self._state_lock:
            self._status_text = text

    # --- worker-thread helpers -----------------------------------

    def wait_if_paused(self) -> None:
        """Block until resumed.  Call inside long-running loops.

        Updates the status text to indicate a paused state and
        restores the previous status on resume.
        """
        if self._pause_event.is_set():
            return
        previous = self.get_status_text()
        self.set_status('Paused.')
        LOGGER.debug('Worker paused.')
        self._pause_event.wait()
        LOGGER.debug('Worker resumed.')
        self.set_status(previous)

    def is_cancelled(self) -> bool:
        """Return True if cancellation has been requested."""
        return self._cancel_event.is_set()

    def reset_run_state(self) -> None:
        """Reset pause/cancel/progress for a new transition.

        Called by :class:`StateRunner` before spawning the
        worker thread.
        """
        self._pause_event.set()
        self._cancel_event.clear()
        with self._state_lock:
            self._progress = None
            self._status_text = ''
