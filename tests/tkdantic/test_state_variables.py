"""Tests for :mod:`ox_ui.tkdantic.state_variables`."""

import logging
import threading
from typing import List

from pydantic import BaseModel, Field

from ox_ui.tkdantic.callbacks import SimpleCallback
from ox_ui.tkdantic.state_variables import StateVariables


class Settings(BaseModel):
    """User settings with a mutable list field."""

    level: int = 1
    names: List[str] = Field(default_factory=list)


class Recorder:
    """Callable that records each call's args and kwargs."""

    def __init__(self):
        """Start with no recorded calls."""
        self.calls = []

    def __call__(self, *args, **kwargs):
        """Record one call."""
        self.calls.append((args, kwargs))


def raise_error():
    """Callback that always fails."""
    raise RuntimeError('callback failed')


class Reader:
    """Callback that reads the variables it is attached to.

    Calling :meth:`StateVariables.get` from inside a callback
    deadlocks if callbacks run while the lock is held.
    """

    def __init__(self, state_vars):
        """Remember the StateVariables to read from."""
        self.state_vars = state_vars
        self.seen = []

    def __call__(self):
        """Read the current variables and remove itself."""
        self.seen.append(self.state_vars.get())
        self.state_vars.remove_changed_callback('reader')


class TestGetSet:
    """Tests for :meth:`get` and :meth:`set`."""

    def test_initially_none(self):
        """A new instance holds no variables."""
        assert StateVariables().get() is None

    def test_round_trip(self):
        """The stored value equals the value that was set."""
        sv = StateVariables()
        sv.set(Settings(level=3, names=['a']))
        assert sv.get() == Settings(level=3, names=['a'])

    def test_set_stores_a_copy(self):
        """Mutating the caller's model does not change the store."""
        sv = StateVariables()
        original = Settings(names=['a'])
        sv.set(original)
        original.names.append('b')
        assert sv.get().names == ['a']

    def test_get_returns_a_copy(self):
        """Mutating a returned snapshot does not change the store."""
        sv = StateVariables()
        sv.set(Settings(names=['a']))
        snapshot = sv.get()
        snapshot.names.append('b')
        assert sv.get().names == ['a']


class TestCallbacks:
    """Tests for the changed-callback registry."""

    def test_set_fires_callbacks(self):
        """Each set() call runs callbacks with their args."""
        sv = StateVariables()
        rec = Recorder()
        sv.add_changed_callback('r', SimpleCallback(
            function=rec, args=[1], kwargs={'k': 2}))
        sv.set(Settings())
        sv.set(Settings(level=2))
        assert rec.calls == [((1,), {'k': 2})] * 2

    def test_replace_by_name(self):
        """Adding a callback under an existing name replaces it."""
        sv = StateVariables()
        first, second = Recorder(), Recorder()
        sv.add_changed_callback('r', SimpleCallback(function=first))
        sv.add_changed_callback('r', SimpleCallback(function=second))
        sv.execute_changed_callbacks()
        assert not first.calls
        assert second.calls == [((), {})]

    def test_remove(self):
        """A removed callback no longer fires; unknown names are ok."""
        sv = StateVariables()
        rec = Recorder()
        sv.add_changed_callback('r', SimpleCallback(function=rec))
        sv.remove_changed_callback('r')
        sv.remove_changed_callback('never-added')
        sv.set(Settings())
        assert not rec.calls

    def test_error_does_not_stop_others(self, caplog):
        """A failing callback is logged and later ones still run."""
        sv = StateVariables()
        rec = Recorder()
        sv.add_changed_callback(
            'bad', SimpleCallback(function=raise_error))
        sv.add_changed_callback('good', SimpleCallback(function=rec))
        with caplog.at_level(logging.ERROR):
            sv.set(Settings())
        assert rec.calls == [((), {})]
        assert 'state-changed callback' in caplog.text

    def test_callback_may_use_instance(self):
        """A callback can call get() and remove itself."""
        sv = StateVariables()
        reader = Reader(sv)
        sv.add_changed_callback(
            'reader', SimpleCallback(function=reader))
        worker = threading.Thread(
            target=sv.set, args=(Settings(level=9),), daemon=True)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), 'set() deadlocked'
        assert reader.seen == [Settings(level=9)]
        sv.set(Settings())
        assert len(reader.seen) == 1
