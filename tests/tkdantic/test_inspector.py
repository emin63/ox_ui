"""Tests for :mod:`ox_ui.tkdantic.inspector`."""

from pydantic import BaseModel
import pytest

from ox_ui.tkdantic.inspector import commands_for_cls, pydantic_method


class Trip(BaseModel):
    """A trip to plan."""

    origin: str
    seats: int = 1


class Planner:
    """Class with a mix of marked and unmarked methods."""

    def __init__(self):
        """Start with no recorded trips."""
        self.trips = []

    @pydantic_method
    def plan(self, trip: Trip):
        """Plan a trip."""
        self.trips.append(trip)
        return trip.seats

    @pydantic_method
    def hello(self):
        return 'demo'

    def helper(self, trip: Trip):
        """Not marked, so not a command."""
        return trip


class TwoModels:
    """Class whose marked method takes two models."""

    @pydantic_method
    def both(self, first: Trip, second: Trip):
        """Take two trips."""
        return first, second


@pydantic_method
def free_function(trip: Trip, label: str):
    """A marked function without ``self``."""
    return trip.origin, label


class TestPydanticMethod:
    """Tests for the :func:`pydantic_method` decorator."""

    def test_records_model_params(self):
        """The wrapper records the model-typed parameters."""
        assert Planner.plan._pydantic_params == {'trip': Trip}
        assert Planner.hello._pydantic_params == {}

    def test_json_string_is_parsed(self):
        """A JSON string argument is validated into the model."""
        planner = Planner()
        result = planner.plan('{"origin": "Oslo", "seats": 5}')
        assert result == 5
        assert planner.trips == [Trip(origin='Oslo', seats=5)]

    def test_model_instance_passed_through(self):
        """A model instance is passed to the method unchanged."""
        planner = Planner()
        trip = Trip(origin='X')
        assert planner.plan(trip) == 1
        assert planner.trips[0] is trip

    def test_function_without_self(self):
        """A plain function is wrapped and its JSON arg parsed."""
        assert free_function('{"origin": "Lima"}', 'note') == (
            'Lima', 'note')

    def test_non_model_string_not_parsed(self):
        """A string for a non-model parameter stays a string."""
        assert free_function(Trip(origin='B'), '{"x": 1}') == (
            'B', '{"x": 1}')

    def test_keeps_name_and_doc(self):
        """functools.wraps keeps the name and docstring."""
        assert Planner.plan.__name__ == 'plan'
        assert Planner.plan.__doc__ == 'Plan a trip.'


class TestCommandsForCls:
    """Tests for :func:`commands_for_cls`."""

    def test_only_marked_methods(self):
        """Only decorated methods become commands, sorted by name."""
        commands = commands_for_cls(Planner)
        assert [c.title for c in commands] == ['hello', 'plan']

    def test_command_fields(self):
        """Commands carry parameters, description and a name."""
        by_title = {c.title: c for c in commands_for_cls(Planner)}
        plan = by_title['plan']
        assert plan.parameters == [Trip]
        assert plan.description == 'Plan a trip.'
        assert plan.name == 'plan'
        assert by_title['hello'].parameters == []
        assert by_title['hello'].description == 'No doc provided.'

    def test_multiple_models_rejected(self):
        """A method taking two models cannot become a command."""
        with pytest.raises(AssertionError):
            commands_for_cls(TwoModels)
