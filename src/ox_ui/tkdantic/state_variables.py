"""Thread-safe state-variables holder for :class:`RunnableMachineHelper`.

This module defines :class:`StateVariables`, a small thread-safe
object that owns the user-configurable pydantic model exposed by a
runnable machine, along with the set of named callbacks that should
fire whenever that model is replaced.

A :class:`StateVariables` instance can be shared: a
:class:`RunnableMachineHelper` holds one as its public
``.state_vars`` attribute, and can pass the same instance to helper
functions or other objects so they can read / write state variables
without being given access to the full helper.

Mirrors the design of :class:`Status` and :class:`RunController`:

* the internal lock is private,
* reads return deep copies so callers cannot mutate the stored model,
* writes store a deep copy for the same reason,
* changed-callbacks are registered by name and fired under snapshot
  (the callback dict is copied under the lock, then released before
  any callback runs, so callbacks may safely call :meth:`get` or
  register / remove other callbacks without deadlocking).
"""

import logging
import threading
from typing import Optional

from pydantic import BaseModel

from ox_ui.tkdantic.callbacks import SimpleCallback

LOGGER = logging.getLogger(__name__)


class StateVariables:
    """Thread-safe container for a pydantic state-variables model.

    All public methods acquire an internal lock, so instances are
    safe to share across threads.  The lock is private; do not
    rely on being able to acquire it externally.
    """

    def __init__(self) -> None:
        """Initialise with no variables and no callbacks."""
        self._lock = threading.Lock()
        self._vars: Optional[BaseModel] = None
        self._callbacks: dict[str, SimpleCallback] = {}

    # --- vars access -------------------------------------------------

    def get(self) -> Optional[BaseModel]:
        """Return a deep copy of the current state variables.

        Returns ``None`` if no variables have been set.  The
        returned instance is a **snapshot**; callers should not
        mutate it and expect the change to be visible to other
        holders of this :class:`StateVariables` instance.
        """
        with self._lock:
            if self._vars is None:
                return None
            return self._vars.model_copy(deep=True)

    def set(self, variables: BaseModel) -> None:
        """Replace the current state variables wholesale.

        A deep copy of *variables* is stored so the caller
        retains no shared reference.  After storing, all
        registered changed-callbacks are executed.

        Callers should only invoke this when the machine is
        idle or paused.

        :param variables: validated pydantic model instance.
        """
        with self._lock:
            self._vars = variables.model_copy(deep=True)
        self.execute_changed_callbacks()

    # --- changed-callbacks -------------------------------------------

    def add_changed_callback(
        self, name: str, callback: SimpleCallback,
    ) -> None:
        """Register a named callback for state-variable changes.

        If *name* already exists it is replaced.

        :param name: unique key for this callback.
        :param callback: :class:`SimpleCallback` to invoke.
        """
        with self._lock:
            self._callbacks[name] = callback

    def remove_changed_callback(self, name: str) -> None:
        """Remove a previously registered callback by name.

        Silently ignores unknown names.

        :param name: key passed to :meth:`add_changed_callback`.
        """
        with self._lock:
            self._callbacks.pop(name, None)

    def execute_changed_callbacks(self) -> None:
        """Run all registered changed-callbacks.

        The callback dict is snapshotted under the lock and then
        released **before** any callback executes.  This prevents
        deadlocks when a callback calls :meth:`get` or registers /
        removes other callbacks.

        Exceptions in individual callbacks are logged and do not
        prevent remaining callbacks from running.
        """
        with self._lock:
            snapshot = list(self._callbacks.values())
        for cb in snapshot:
            try:
                cb.function(
                    *(cb.args or []),
                    **(cb.kwargs or {}),
                )
            except Exception:
                LOGGER.exception(
                    'Error in state-changed callback.',
                )
