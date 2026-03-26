"""Tests for state_runner and runnable modules.

These tests exercise :class:`StateRunner` and
:class:`RunnableMachineHelper` without requiring a display.
All tests use pytest conventions.
"""

import threading
import time

import pytest
from transitions import Machine

from ox_ui.tkdantic.runnable import (
    RunnableMachine,
    RunnableMachineHelper,
)
from ox_ui.tkdantic.state_runner import StateRunner


# -------------------------------------------------------------------
# Test fixtures / helpers
# -------------------------------------------------------------------

class SimplePipeline(RunnableMachineHelper):
    """A minimal 3-state machine for testing.

    The ``start`` trigger enters ``processing`` where a
    configurable number of sub-tasks run with a short sleep.
    """

    STATES = ['idle', 'processing', 'done']
    TRANSITIONS = [
        {'trigger': 'start', 'source': 'idle',
         'dest': 'processing'},
        {'trigger': 'finish', 'source': 'processing',
         'dest': 'done'},
        {'trigger': 'reset', 'source': 'done',
         'dest': 'idle'},
    ]

    def __init__(self, num_steps=5, step_delay=0.02):
        """Set up the pipeline with configurable steps."""
        super().__init__()
        self.num_steps = num_steps
        self.step_delay = step_delay
        self.steps_completed = 0
        self._machine = Machine(
            model=self,
            states=self.STATES,
            transitions=self.TRANSITIONS,
            initial='idle',
        )

    def get_machine(self):
        """Return the transitions Machine."""
        return self._machine

    def on_enter_processing(self):
        """Simulate long-running work with sub-tasks."""
        self.steps_completed = 0
        for i in range(self.num_steps):
            self.wait_if_paused()
            if self.is_cancelled():
                self.set_status('Cancelled.')
                return
            time.sleep(self.step_delay)
            self.steps_completed = i + 1
            self.set_progress(
                self.steps_completed / self.num_steps,
            )
            self.set_status(
                f'Step {self.steps_completed}'
                f'/{self.num_steps}',
            )


class ErrorPipeline(RunnableMachineHelper):
    """A machine whose on_enter_B raises an exception."""

    def __init__(self):
        """Set up a machine that errors on enter B."""
        super().__init__()
        self._machine = Machine(
            model=self,
            states=['A', 'B'],
            transitions=[
                {'trigger': 'go', 'source': 'A', 'dest': 'B'},
            ],
            initial='A',
        )

    def get_machine(self):
        """Return the transitions Machine."""
        return self._machine

    def on_enter_B(self):
        """Raise to simulate a failure."""
        raise RuntimeError('intentional test error')


class MinimalMachine(RunnableMachineHelper):
    """Machine with no long-running work."""

    def __init__(self):
        """Set up a simple two-state machine."""
        super().__init__()
        self._machine = Machine(
            model=self,
            states=['X', 'Y'],
            transitions=[
                {'trigger': 'go', 'source': 'X', 'dest': 'Y'},
                {'trigger': 'back', 'source': 'Y',
                 'dest': 'X'},
            ],
            initial='X',
        )

    def get_machine(self):
        """Return the transitions Machine."""
        return self._machine


@pytest.fixture
def pipeline():
    """Create a SimplePipeline and StateRunner."""
    p = SimplePipeline(num_steps=5, step_delay=0.02)
    return p, StateRunner(p)


@pytest.fixture
def error_pipeline():
    """Create an ErrorPipeline and StateRunner."""
    p = ErrorPipeline()
    return p, StateRunner(p)


@pytest.fixture
def minimal():
    """Create a MinimalMachine and StateRunner."""
    m = MinimalMachine()
    return m, StateRunner(m)


# -------------------------------------------------------------------
# Protocol conformance
# -------------------------------------------------------------------

class TestProtocol:
    """Verify that helpers satisfy the Protocol."""

    def test_helper_is_runnable(self):
        """RunnableMachineHelper satisfies RunnableMachine."""
        p = SimplePipeline()
        assert isinstance(p, RunnableMachine)

    def test_minimal_is_runnable(self):
        """MinimalMachine satisfies RunnableMachine."""
        m = MinimalMachine()
        assert isinstance(m, RunnableMachine)


# -------------------------------------------------------------------
# State queries
# -------------------------------------------------------------------

class TestStateQueries:
    """Test StateRunner state inspection methods."""

    def test_initial_state(self, pipeline):
        """Runner reports the correct initial state."""
        _, runner = pipeline
        assert runner.get_current_state() == 'idle'

    def test_all_states(self, pipeline):
        """Runner lists all defined states."""
        _, runner = pipeline
        states = runner.get_all_states()
        assert set(states) == {'idle', 'processing', 'done'}

    def test_available_triggers_filters_auto(self, pipeline):
        """Auto-transitions (to_X) are filtered out."""
        _, runner = pipeline
        triggers = runner.get_available_triggers()
        assert 'start' in triggers
        for s in runner.get_all_states():
            assert f'to_{s}' not in triggers

    def test_available_triggers_from_idle(self, pipeline):
        """Only 'start' is available from idle."""
        _, runner = pipeline
        triggers = runner.get_available_triggers()
        assert triggers == ['start']


# -------------------------------------------------------------------
# Basic transitions
# -------------------------------------------------------------------

class TestBasicTransitions:
    """Test that transitions complete correctly."""

    def test_simple_transition(self, minimal):
        """Trigger 'go' moves from X to Y."""
        machine, runner = minimal
        runner.start_transition('go')
        _wait_for_idle(runner)
        assert runner.get_current_state() == 'Y'

    def test_round_trip(self, minimal):
        """Go X->Y->X and verify state each time."""
        machine, runner = minimal
        runner.start_transition('go')
        _wait_for_idle(runner)
        assert runner.get_current_state() == 'Y'

        runner.start_transition('back')
        _wait_for_idle(runner)
        assert runner.get_current_state() == 'X'

    def test_transition_with_work(self, pipeline):
        """Pipeline runs through processing steps."""
        p, runner = pipeline
        runner.start_transition('start')
        _wait_for_idle(runner)
        assert runner.get_current_state() == 'processing'
        assert p.steps_completed == p.num_steps
        assert p.get_progress() == 1.0


# -------------------------------------------------------------------
# Progress and status
# -------------------------------------------------------------------

class TestProgressStatus:
    """Test progress and status reporting."""

    def test_initial_progress_is_none(self, pipeline):
        """Progress starts as None."""
        p, _ = pipeline
        assert p.get_progress() is None

    def test_initial_status_empty(self, pipeline):
        """Status starts empty."""
        p, _ = pipeline
        assert p.get_status_text() == ''

    def test_progress_advances(self, pipeline):
        """Progress reaches 1.0 after completion."""
        p, runner = pipeline
        runner.start_transition('start')
        _wait_for_idle(runner)
        assert p.get_progress() == 1.0

    def test_status_set_during_work(self, pipeline):
        """Status is non-empty after work completes."""
        p, runner = pipeline
        runner.start_transition('start')
        _wait_for_idle(runner)
        assert 'Step' in p.get_status_text()


# -------------------------------------------------------------------
# Pause and resume
# -------------------------------------------------------------------

class TestPauseResume:
    """Test cooperative pause and resume."""

    def test_pause_freezes_progress(self):
        """Pausing stops progress from advancing."""
        p = SimplePipeline(num_steps=20, step_delay=0.02)
        runner = StateRunner(p)
        runner.start_transition('start')

        # Let a few steps run then pause.
        time.sleep(0.08)
        runner.request_pause()
        time.sleep(0.06)

        frozen = p.steps_completed
        assert frozen > 0, 'Should have done some work.'
        assert frozen < 20, 'Should not be finished.'

        # Verify progress is frozen.
        time.sleep(0.06)
        assert p.steps_completed == frozen

        # Resume and wait for completion.
        runner.request_resume()
        _wait_for_idle(runner)
        assert p.steps_completed == 20

    def test_pause_status_message(self):
        """Status shows 'Paused.' while paused."""
        p = SimplePipeline(num_steps=20, step_delay=0.03)
        runner = StateRunner(p)
        runner.start_transition('start')

        time.sleep(0.08)
        runner.request_pause()
        time.sleep(0.08)

        assert p.get_status_text() == 'Paused.'

        runner.request_resume()
        _wait_for_idle(runner)


# -------------------------------------------------------------------
# Cancel
# -------------------------------------------------------------------

class TestCancel:
    """Test cooperative cancellation."""

    def test_cancel_stops_work(self):
        """Cancelling prevents all steps from completing."""
        p = SimplePipeline(num_steps=50, step_delay=0.02)
        runner = StateRunner(p)
        runner.start_transition('start')

        time.sleep(0.06)
        runner.request_cancel()
        _wait_for_idle(runner)

        assert p.steps_completed < 50
        assert 'Cancel' in p.get_status_text()


# -------------------------------------------------------------------
# Locking / busy
# -------------------------------------------------------------------

class TestLocking:
    """Test that concurrent transitions are prevented."""

    def test_is_busy_during_transition(self):
        """is_busy() returns True while worker runs."""
        p = SimplePipeline(num_steps=10, step_delay=0.02)
        runner = StateRunner(p)
        runner.start_transition('start')
        assert runner.is_busy()
        _wait_for_idle(runner)
        assert not runner.is_busy()

    def test_start_returns_false_when_busy(self):
        """Cannot start a second transition while busy."""
        p = SimplePipeline(num_steps=10, step_delay=0.02)
        runner = StateRunner(p)
        runner.start_transition('start')
        result = runner.start_transition('start')
        assert result is False
        _wait_for_idle(runner)


# -------------------------------------------------------------------
# Exception handling
# -------------------------------------------------------------------

class TestExceptionHandling:
    """Test that exceptions during transitions are captured."""

    def test_exception_stored(self, error_pipeline):
        """Runner captures the exception in last_error."""
        _, runner = error_pipeline
        runner.start_transition('go')
        _wait_for_idle(runner)
        assert runner.last_error is not None
        assert 'intentional' in str(runner.last_error)

    def test_state_after_on_enter_error(self, error_pipeline):
        """State changes even if on_enter raises.

        pytransitions changes state *before* calling on_enter,
        so the state is 'B' despite the error.
        """
        _, runner = error_pipeline
        runner.start_transition('go')
        _wait_for_idle(runner)
        assert runner.get_current_state() == 'B'

    def test_error_clears_on_next_transition(self, minimal):
        """last_error resets when a new transition starts."""
        machine, runner = minimal
        runner.last_error = RuntimeError('stale')
        runner.start_transition('go')
        _wait_for_idle(runner)
        assert runner.last_error is None


# -------------------------------------------------------------------
# Reset run state
# -------------------------------------------------------------------

class TestResetRunState:
    """Test that reset_run_state clears previous state."""

    def test_reset_clears_cancel(self):
        """After reset, is_cancelled returns False."""
        p = SimplePipeline()
        p.cancel()
        assert p.is_cancelled()
        p.reset_run_state()
        assert not p.is_cancelled()

    def test_reset_clears_progress(self):
        """After reset, progress is None."""
        p = SimplePipeline()
        p.set_progress(0.5)
        p.reset_run_state()
        assert p.get_progress() is None

    def test_reset_clears_status(self):
        """After reset, status is empty."""
        p = SimplePipeline()
        p.set_status('hello')
        p.reset_run_state()
        assert p.get_status_text() == ''


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _wait_for_idle(
    runner: StateRunner,
    timeout: float = 5.0,
) -> None:
    """Block until the runner is no longer busy.

    :raises TimeoutError: if *timeout* seconds elapse.
    """
    deadline = time.monotonic() + timeout
    while runner.is_busy():
        if time.monotonic() > deadline:
            raise TimeoutError(
                'Runner did not finish within '
                f'{timeout}s.',
            )
        time.sleep(0.01)
