"""Cooperative pause/resume/cancel controller for worker threads.

This module defines :class:`RunController`, a small thread-safe
object that owns the primitives used to cooperatively pause and
cancel a long-running worker:

* a pause :class:`threading.Event` (set means "running"),
* a cancel :class:`threading.Event` (set means "cancel requested").

A :class:`RunController` instance can be shared: a
:class:`RunnableMachineHelper` holds one as its public
``.controller`` attribute, and can pass the same instance to
helper functions or other objects so they can honour pause /
cancel without being given access to the full helper.

The :class:`threading.Event` primitives used internally are
themselves thread-safe, so :class:`RunController` does not
need an additional lock.
"""

import logging
import threading
from typing import Optional

from ox_ui.tkdantic.status_manager import Status

LOGGER = logging.getLogger(__name__)


class RunController:
    """Thread-safe cooperative pause / cancel controller.

    Call :meth:`pause`, :meth:`resume`, and :meth:`cancel` from
    any thread (typically the UI thread).  Call
    :meth:`wait_if_paused` and :meth:`is_cancelled` from the
    worker thread inside long-running loops.
    """

    def __init__(self) -> None:
        """Initialise in the running, not-cancelled state."""
        # Set means "running" (not paused).
        self._pause_event = threading.Event()
        self._pause_event.set()

        # Set means "cancel requested".
        self._cancel_event = threading.Event()

    # --- control (any thread) -----------------------------------

    def pause(self) -> None:
        """Request the worker to pause at the next safe point."""
        self._pause_event.clear()
        LOGGER.debug('Pause requested.')

    def resume(self) -> None:
        """Resume a paused worker."""
        self._pause_event.set()
        LOGGER.debug('Resume requested.')

    def cancel(self) -> None:
        """Request cooperative cancellation.

        Also releases any pause so the worker can observe the
        cancel request on its next :meth:`is_cancelled` check.
        """
        self._cancel_event.set()
        self._pause_event.set()
        LOGGER.debug('Cancel requested.')

    # --- worker-thread helpers ----------------------------------

    def is_cancelled(self) -> bool:
        """Return True if cancellation has been requested."""
        return self._cancel_event.is_set()

    def wait_if_paused(
        self, status: Optional[Status] = None,
    ) -> None:
        """Block until resumed.  Call inside long-running loops.

        If *status* is provided, the raw status text is set to
        ``'Paused.'`` for the duration of the pause and the
        previous raw text is restored on resume.  Status
        headers are not affected.  If *status* is ``None``
        the method simply blocks; no status updates occur.

        :param status: optional :class:`Status` to update
            during the pause.
        """
        if self._pause_event.is_set():
            return

        previous: Optional[str] = None
        if status is not None:
            previous = status.get_raw_status()
            status.set_status('Paused.')

        LOGGER.debug('Worker paused.')
        self._pause_event.wait()
        LOGGER.debug('Worker resumed.')

        if status is not None and previous is not None:
            status.set_status(previous)

    # --- lifecycle ----------------------------------------------

    def reset(self) -> None:
        """Reset to the running, not-cancelled state.

        Intended for use when resetting run state before a
        new transition.
        """
        self._pause_event.set()
        self._cancel_event.clear()
