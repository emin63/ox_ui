"""Tests for :class:`ox_ui.tkdantic.runnable.RunnableMachineHelper`.

The thread-driven behaviour is covered in ``test_state_runner.py``;
these tests check the delegation to the shared Status,
RunController and StateVariables objects.
"""

from pydantic import BaseModel
import pytest

from ox_ui.tkdantic.callbacks import SimpleCallback
from ox_ui.tkdantic.run_controller import RunController
from ox_ui.tkdantic.runnable import (
    RunnableMachine,
    RunnableMachineHelper,
)
from ox_ui.tkdantic.state_variables import StateVariables
from ox_ui.tkdantic.status_manager import Status


class Knobs(BaseModel):
    """State variables for the tests."""

    speed: int = 1


class Recorder:
    """Callable that counts how often it is called."""

    def __init__(self):
        """Start with a zero count."""
        self.count = 0

    def __call__(self):
        """Count one call."""
        self.count += 1


class TestAbstract:
    """The helper must be subclassed."""

    def test_get_machine_not_implemented(self):
        """The base get_machine raises NotImplementedError."""
        with pytest.raises(NotImplementedError):
            RunnableMachineHelper().get_machine()

    def test_satisfies_protocol(self):
        """The helper satisfies the RunnableMachine protocol."""
        helper = RunnableMachineHelper()
        assert isinstance(helper, RunnableMachine)
        assert helper.get_model() is helper


class TestSharedObjects:
    """Status, controller and state_vars can be passed in."""

    def test_defaults_are_fresh(self):
        """Without arguments each helper gets its own objects."""
        one, two = RunnableMachineHelper(), RunnableMachineHelper()
        assert one.status is not two.status
        assert one.controller is not two.controller
        assert one.state_vars is not two.state_vars

    def test_shared_instances_used(self):
        """Objects passed in are used as is."""
        status = Status()
        controller = RunController()
        state_vars = StateVariables()
        helper = RunnableMachineHelper(
            status=status, controller=controller,
            state_vars=state_vars)
        assert helper.status is status
        assert helper.controller is controller
        assert helper.state_vars is state_vars


class TestStatusDelegation:
    """Status methods write to the shared Status."""

    def test_status_methods(self):
        """set/append/header calls are visible on the Status."""
        status = Status()
        helper = RunnableMachineHelper(status=status)
        helper.set_status('a')
        helper.append_status('b')
        helper.add_status_header('h', 'Header')
        assert status.get_status_text() == 'Header\na\nb'
        assert helper.get_raw_status() == 'a\nb'
        helper.remove_status_header('h')
        assert helper.get_status_text() == 'a\nb'

    def test_progress(self):
        """Progress is stored and returned."""
        helper = RunnableMachineHelper()
        helper.set_progress(0.25)
        assert helper.get_progress() == 0.25

    def test_reset_run_state(self):
        """reset_run_state clears progress, status and cancel."""
        helper = RunnableMachineHelper()
        helper.set_progress(0.5)
        helper.set_status('busy')
        helper.add_status_header('h', 'H')
        helper.cancel()
        helper.reset_run_state()
        assert helper.get_progress() is None
        assert helper.get_status_text() == ''
        assert not helper.is_cancelled()


class TestControlDelegation:
    """pause/resume/cancel act on the shared controller."""

    def test_cancel(self):
        """cancel() is visible through the controller."""
        controller = RunController()
        helper = RunnableMachineHelper(controller=controller)
        helper.cancel()
        assert controller.is_cancelled()
        assert helper.is_cancelled()

    def test_pause_then_resume(self):
        """After pause and resume, wait_if_paused returns at once."""
        helper = RunnableMachineHelper()
        helper.pause()
        helper.resume()
        helper.set_status('kept')
        helper.wait_if_paused()
        assert helper.get_raw_status() == 'kept'


class TestStateVariableDelegation:
    """State-variable methods act on the shared StateVariables."""

    def test_default_is_none(self):
        """A helper has no state variables by default."""
        assert RunnableMachineHelper().get_state_variables() is None

    def test_set_get_and_callbacks(self):
        """set_state_variables stores a copy and fires callbacks."""
        state_vars = StateVariables()
        helper = RunnableMachineHelper(state_vars=state_vars)
        rec = Recorder()
        helper.add_state_changed_callback(
            'r', SimpleCallback(function=rec))
        helper.set_state_variables(Knobs(speed=4))
        assert state_vars.get() == Knobs(speed=4)
        assert helper.get_state_variables() == Knobs(speed=4)
        helper.execute_state_changed_callbacks()
        assert rec.count == 2
        helper.remove_state_changed_callback('r')
        helper.set_state_variables(Knobs())
        assert rec.count == 2
