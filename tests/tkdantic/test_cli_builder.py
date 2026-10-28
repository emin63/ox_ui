"""Tests for :mod:`ox_ui.tkdantic.cli_builder`.

Each test method has a docstring showing the equivalent
command line being exercised.
"""

import json
import socket
import threading
from typing import List, Literal, Optional
from xmlrpc.server import SimpleXMLRPCServer

from click.testing import CliRunner
from pydantic import BaseModel, ConfigDict, Field, computed_field
import pytest

from ox_ui.tkdantic import cli_builder
from ox_ui.tkdantic.cli_builder import (
    click_command_from_model,
    click_group_from_commands,
    click_group_from_cls,
    _make_flag_and_param,
    _reassemble_kwargs,
)
from ox_ui.tkdantic.command import Command
from ox_ui.tkdantic.inspector import pydantic_method


# ---------------------------------------------------------------
# Test models
# ---------------------------------------------------------------

class SimpleTrip(BaseModel):
    """A simple trip with basic scalar fields."""

    traveler: str
    seats: int
    speed: float = 9.99


class Address(BaseModel):
    """Mailing address."""

    street: str
    city: str
    zip_code: str = '00000'


class TripWithAddress(BaseModel):
    """A trip containing a nested address sub-model."""

    traveler: str
    address: Address


class Stop(BaseModel):
    """One stop on a route."""

    miles: float
    month: str


class Route(BaseModel):
    """A route with several stops."""

    origin: str
    stops: List[Stop]


class FlagModel(BaseModel):
    """Model with a boolean flag."""

    name: str
    verbose: bool = False


class ChoiceModel(BaseModel):
    """Model with a Literal choice field."""

    color: Literal['red', 'green', 'blue'] = 'red'
    size: int = 10


class NullableModel(BaseModel):
    """Model with an optional nullable field."""

    name: str
    nickname: Optional[str] = None


class Inner(BaseModel):
    """Deeply nested inner model."""

    value: int


class Middle(BaseModel):
    """Middle layer wrapping an inner model."""

    label: str
    inner: Inner


class Deep(BaseModel):
    """Three-level nesting test model."""

    top_name: str
    middle: Middle


class MixedModel(BaseModel):
    """Model with both nested model and model_list."""

    title: str
    address: Address
    stops: List[Stop]


# ---------------------------------------------------------------
# Helper to capture the validated model instance
# ---------------------------------------------------------------

class Capture:
    """Callable that stores whatever it receives."""

    def __init__(self):
        self.instance = None

    def __call__(self, inst):
        self.instance = inst
        return 'OK'


# ---------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------

class TestMakeFlagAndParam:
    """Tests for _make_flag_and_param."""

    def test_no_prefix(self):
        """--traveler → traveler"""
        flag, param = _make_flag_and_param('', 'traveler')
        assert flag == '--traveler'
        assert param == 'traveler'

    def test_underscore_in_name(self):
        """--zip-code → zip_code"""
        flag, param = _make_flag_and_param('', 'zip_code')
        assert flag == '--zip-code'
        assert param == 'zip_code'

    def test_with_prefix(self):
        """--address.city → address__city"""
        flag, param = _make_flag_and_param('address', 'city')
        assert flag == '--address.city'
        assert param == 'address__city'

    def test_nested_prefix(self):
        """--middle.inner.value → middle__inner__value"""
        flag, param = _make_flag_and_param(
            'middle__inner', 'value',
        )
        assert flag == '--middle.inner.value'
        assert param == 'middle__inner__value'


class TestReassembleKwargs:
    """Tests for _reassemble_kwargs."""

    def test_flat(self):
        """Simple flat kwargs pass through."""
        result = _reassemble_kwargs(
            {'name': 'Alice', 'age': 30}, {},
        )
        assert result == {'name': 'Alice', 'age': 30}

    def test_nesting(self):
        """Dunder-separated keys become nested dicts."""
        result = _reassemble_kwargs(
            {'address__city': 'Boston', 'name': 'Bob'}, {},
        )
        assert result == {
            'name': 'Bob',
            'address': {'city': 'Boston'},
        }

    def test_none_skipped(self):
        """None values are omitted for pydantic defaults."""
        result = _reassemble_kwargs(
            {'name': 'Eve', 'age': None}, {},
        )
        assert result == {'name': 'Eve'}

    def test_model_list_parsed(self):
        """Model list params are JSON-parsed."""
        raw = {
            'stops': (
                '{"miles": 100, "month": "Jan"}',
                '{"miles": 200, "month": "Feb"}',
            ),
        }
        result = _reassemble_kwargs(raw, {'stops': object})
        assert len(result['stops']) == 2
        assert result['stops'][0]['miles'] == 100


# ---------------------------------------------------------------
# Integration tests: click_command_from_model
# ---------------------------------------------------------------

class TestSimpleModel:
    """Tests for a flat model with str, int, float fields."""

    def test_all_options_provided(self):
        """mycli --traveler Alice --seats 5 --speed 12.50"""
        cap = Capture()
        cmd = click_command_from_model(
            SimpleTrip, callback=cap, name='trip',
        )
        result = CliRunner().invoke(cmd, [
            '--traveler', 'Alice',
            '--seats', '5',
            '--speed', '12.50',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.traveler == 'Alice'
        assert cap.instance.seats == 5
        assert cap.instance.speed == 12.50

    def test_default_value_used(self):
        """mycli --traveler Bob --seats 3

        The --speed option is omitted; default 9.99 applies.
        """
        cap = Capture()
        cmd = click_command_from_model(
            SimpleTrip, callback=cap, name='trip',
        )
        result = CliRunner().invoke(cmd, [
            '--traveler', 'Bob',
            '--seats', '3',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.speed == 9.99

    def test_missing_required_option(self):
        """mycli --traveler Alice

        Missing --seats should produce an error.
        """
        cap = Capture()
        cmd = click_command_from_model(
            SimpleTrip, callback=cap, name='trip',
        )
        result = CliRunner().invoke(cmd, [
            '--traveler', 'Alice',
        ])
        assert result.exit_code != 0
        assert 'seats' in result.output.lower()

    def test_callback_return_echoed(self):
        """mycli --traveler X --seats 1

        The callback's return value is echoed to stdout.
        """
        cap = Capture()
        cmd = click_command_from_model(
            SimpleTrip, callback=cap, name='trip',
        )
        result = CliRunner().invoke(cmd, [
            '--traveler', 'X',
            '--seats', '1',
        ])
        assert 'OK' in result.output


class TestNestedModel:
    """Tests for a model with a nested BaseModel field."""

    def test_dotted_options(self):
        """mycli --traveler Eve --address.street '123 Main' \\
               --address.city Boston
        """
        cap = Capture()
        cmd = click_command_from_model(
            TripWithAddress, callback=cap, name='trip',
        )
        result = CliRunner().invoke(cmd, [
            '--traveler', 'Eve',
            '--address.street', '123 Main St',
            '--address.city', 'Boston',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.address.street == '123 Main St'
        assert cap.instance.address.city == 'Boston'

    def test_nested_default_applied(self):
        """mycli --traveler Eve --address.street '123 Main' \\
               --address.city Boston

        The zip_code sub-field defaults to '00000'.
        """
        cap = Capture()
        cmd = click_command_from_model(
            TripWithAddress, callback=cap, name='trip',
        )
        result = CliRunner().invoke(cmd, [
            '--traveler', 'Eve',
            '--address.street', '123 Main',
            '--address.city', 'Boston',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.address.zip_code == '00000'


class TestDeepNesting:
    """Tests for three-level nesting."""

    def test_deep_dotted_options(self):
        """mycli --top-name Root \\
               --middle.label Mid \\
               --middle.inner.value 42
        """
        cap = Capture()
        cmd = click_command_from_model(
            Deep, callback=cap, name='deep',
        )
        result = CliRunner().invoke(cmd, [
            '--top-name', 'Root',
            '--middle.label', 'Mid',
            '--middle.inner.value', '42',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.top_name == 'Root'
        assert cap.instance.middle.label == 'Mid'
        assert cap.instance.middle.inner.value == 42


class TestModelList:
    """Tests for List[BaseModel] fields (repeated JSON)."""

    def test_repeated_json(self):
        """mycli --origin Oslo \\
               --stops '{"miles":100,"month":"Jan"}' \\
               --stops '{"miles":200,"month":"Jun"}'
        """
        cap = Capture()
        cmd = click_command_from_model(
            Route, callback=cap, name='route',
        )
        stop1 = json.dumps({'miles': 100, 'month': 'Jan'})
        stop2 = json.dumps({'miles': 200, 'month': 'Jun'})
        result = CliRunner().invoke(cmd, [
            '--origin', 'Oslo',
            '--stops', stop1,
            '--stops', stop2,
        ])
        assert result.exit_code == 0, result.output
        assert len(cap.instance.stops) == 2
        assert cap.instance.stops[0].miles == 100.0
        assert cap.instance.stops[1].month == 'Jun'

    def test_single_stop(self):
        """mycli --origin Rome \\
               --stops '{"miles":50,"month":"Mar"}'
        """
        cap = Capture()
        cmd = click_command_from_model(
            Route, callback=cap, name='route',
        )
        stop = json.dumps({'miles': 50, 'month': 'Mar'})
        result = CliRunner().invoke(cmd, [
            '--origin', 'Rome', '--stops', stop,
        ])
        assert result.exit_code == 0, result.output
        assert len(cap.instance.stops) == 1


class TestMixedModelAndList:
    """Tests for a model with both nested model and list."""

    def test_mixed(self):
        """mycli --title 'My Route' \\
               --address.street '1 Wall St' \\
               --address.city NYC \\
               --stops '{"miles":100,"month":"Jan"}'
        """
        cap = Capture()
        cmd = click_command_from_model(
            MixedModel, callback=cap, name='mixed',
        )
        stop = json.dumps({'miles': 100, 'month': 'Jan'})
        result = CliRunner().invoke(cmd, [
            '--title', 'My Route',
            '--address.street', '1 Wall St',
            '--address.city', 'NYC',
            '--stops', stop,
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.title == 'My Route'
        assert cap.instance.address.street == '1 Wall St'
        assert cap.instance.stops[0].miles == 100.0


class TestBoolFlag:
    """Tests for boolean flag options."""

    def test_flag_set(self):
        """mycli --name Alice --verbose"""
        cap = Capture()
        cmd = click_command_from_model(
            FlagModel, callback=cap, name='flag',
        )
        result = CliRunner().invoke(cmd, [
            '--name', 'Alice', '--verbose',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.verbose is True

    def test_flag_default(self):
        """mycli --name Bob

        The --verbose flag is omitted; defaults to False.
        """
        cap = Capture()
        cmd = click_command_from_model(
            FlagModel, callback=cap, name='flag',
        )
        result = CliRunner().invoke(cmd, ['--name', 'Bob'])
        assert result.exit_code == 0, result.output
        assert cap.instance.verbose is False


class TestLiteralChoice:
    """Tests for Literal[...] → click.Choice options."""

    def test_valid_choice(self):
        """mycli --color green --size 20"""
        cap = Capture()
        cmd = click_command_from_model(
            ChoiceModel, callback=cap, name='choice',
        )
        result = CliRunner().invoke(cmd, [
            '--color', 'green', '--size', '20',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.color == 'green'

    def test_invalid_choice(self):
        """mycli --color purple --size 10

        'purple' is not in the allowed choices.
        """
        cap = Capture()
        cmd = click_command_from_model(
            ChoiceModel, callback=cap, name='choice',
        )
        result = CliRunner().invoke(cmd, [
            '--color', 'purple',
        ])
        assert result.exit_code != 0
        assert 'purple' in result.output.lower()

    def test_default_choice(self):
        """mycli

        Both fields have defaults; no options needed.
        """
        cap = Capture()
        cmd = click_command_from_model(
            ChoiceModel, callback=cap, name='choice',
        )
        result = CliRunner().invoke(cmd, [])
        assert result.exit_code == 0, result.output
        assert cap.instance.color == 'red'
        assert cap.instance.size == 10


class TestNullableField:
    """Tests for Optional[...] / nullable fields."""

    def test_nullable_omitted(self):
        """mycli --name Alice

        The --nickname option is omitted; defaults to None.
        """
        cap = Capture()
        cmd = click_command_from_model(
            NullableModel, callback=cap, name='nullable',
        )
        result = CliRunner().invoke(cmd, ['--name', 'Alice'])
        assert result.exit_code == 0, result.output
        assert cap.instance.nickname is None

    def test_nullable_provided(self):
        """mycli --name Alice --nickname Ally"""
        cap = Capture()
        cmd = click_command_from_model(
            NullableModel, callback=cap, name='nullable',
        )
        result = CliRunner().invoke(cmd, [
            '--name', 'Alice', '--nickname', 'Ally',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.nickname == 'Ally'


class TestValidationError:
    """Tests that pydantic validation errors surface cleanly."""

    def test_wrong_type(self):
        """mycli --traveler Alice --seats not-a-number

        Click catches the type error before pydantic.
        """
        cap = Capture()
        cmd = click_command_from_model(
            SimpleTrip, callback=cap, name='trip',
        )
        result = CliRunner().invoke(cmd, [
            '--traveler', 'Alice',
            '--seats', 'not-a-number',
        ])
        assert result.exit_code != 0


class TestHelpOutput:
    """Tests that --help is generated correctly."""

    def test_help_shows_options(self):
        """mycli --help

        Should list all options with their types.
        """
        cmd = click_command_from_model(
            SimpleTrip, name='trip',
        )
        result = CliRunner().invoke(cmd, ['--help'])
        assert result.exit_code == 0
        assert '--traveler' in result.output
        assert '--seats' in result.output
        assert '--speed' in result.output

    def test_help_shows_dotted_options(self):
        """mycli --help

        Nested model fields appear as dotted options.
        """
        cmd = click_command_from_model(
            TripWithAddress, name='trip',
        )
        result = CliRunner().invoke(cmd, ['--help'])
        assert '--address.street' in result.output
        assert '--address.city' in result.output

    def test_help_shows_model_list_hint(self):
        """mycli --help

        List[BaseModel] options should mention JSON keys.
        """
        cmd = click_command_from_model(
            Route, name='route',
        )
        result = CliRunner().invoke(cmd, ['--help'])
        assert 'miles' in result.output
        assert 'month' in result.output


# ---------------------------------------------------------------
# Integration tests: click_group_from_commands
# ---------------------------------------------------------------

class TestGroupFromCommands:
    """Tests for building a click.Group from Command objects."""

    def _make_group(self, capture):
        """Build a two-command group for testing."""
        commands = [
            Command(
                title='Plan Trip',
                parameters=[SimpleTrip],
                description='Plan a new trip.',
                callback=capture,
            ),
            Command(
                title='Show Route',
                parameters=[Route],
                description='Display a route.',
                callback=capture,
            ),
        ]
        return click_group_from_commands(commands, name='cli')

    def test_subcommand_listed(self):
        """cli --help

        Both subcommands should appear in the group help.
        """
        cap = Capture()
        group = self._make_group(cap)
        result = CliRunner().invoke(group, ['--help'])
        assert result.exit_code == 0
        assert 'plan_trip' in result.output
        assert 'show_route' in result.output

    def test_invoke_subcommand(self):
        """cli plan_trip --traveler A --seats 1"""
        cap = Capture()
        group = self._make_group(cap)
        result = CliRunner().invoke(group, [
            'plan_trip',
            '--traveler', 'A',
            '--seats', '1',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.traveler == 'A'


# ---------------------------------------------------------------
# Integration tests: click_group_from_cls
# ---------------------------------------------------------------

class TestGroupFromCls:
    """Tests for the convenience class-inspection entry point."""

    def test_discover_methods(self):
        """svc --help

        Methods decorated with @pydantic_method appear as
        subcommands.
        """

        class MyService:
            """Example service."""

            @pydantic_method
            def plan_trip(self, params: SimpleTrip):
                """Plan a new trip."""
                return f'Planned for {params.traveler}'

        group = click_group_from_cls(MyService, name='svc')
        result = CliRunner().invoke(group, ['--help'])
        assert result.exit_code == 0
        assert 'plan_trip' in result.output

    def test_invoke_discovered(self):
        """svc plan_trip --traveler Alice --seats 2

        A method discovered via @pydantic_method is bound
        to an instance and callable through the generated CLI.
        """

        class MyService:
            """Example service."""

            @pydantic_method
            def plan_trip(self, params: SimpleTrip):
                """Plan a new trip."""
                return f'Planned for {params.traveler}'

        group = click_group_from_cls(MyService, name='svc')
        result = CliRunner().invoke(group, [
            'plan_trip',
            '--traveler', 'Alice',
            '--seats', '2',
        ])
        assert result.exit_code == 0, result.output
        assert 'Planned for Alice' in result.output

    def test_with_prebuilt_instance(self):
        """svc plan_trip --traveler Bob --seats 1

        A pre-built instance can be passed explicitly when
        the constructor requires arguments.
        """

        class MyService:
            """Service needing init args."""

            def __init__(self, prefix):
                self.prefix = prefix

            @pydantic_method
            def plan_trip(self, params: SimpleTrip):
                """Plan a new trip."""
                return (
                    f'{self.prefix}: '
                    f'Planned for {params.traveler}'
                )

        svc = MyService(prefix='TEST')
        group = click_group_from_cls(
            MyService, name='svc', instance=svc,
        )
        result = CliRunner().invoke(group, [
            'plan_trip',
            '--traveler', 'Bob',
            '--seats', '1',
        ])
        assert result.exit_code == 0, result.output
        assert 'TEST: Planned for Bob' in result.output


# ---------------------------------------------------------------
# Models and helpers for --params, the on/off flags, scalar lists
# and --rpc-url
# ---------------------------------------------------------------

class Settings(BaseModel):
    """Stage settings, nested in a Job."""

    max_size: int
    step: float = 0.01
    dry_run: bool = False


class Job(BaseModel):
    """A job with nested settings, lists and a flag."""

    name: str
    rounds: int = 1
    verbose: bool = True
    waits: List[float] = Field(default_factory=lambda: [2.0, 4.0])
    stops: List[Stop] = Field(default_factory=list)
    setup: Settings
    color: Optional[Literal['red', 'green']] = None


class ClashingModel(BaseModel):
    """A model whose field takes the --params flag."""

    params: str = ''


class Stage(BaseModel):
    """Settings for one stage."""

    max_size: int = 1
    repeats: int = 1


class WithDefaultStage(BaseModel):
    """A model whose nested field's default is not the class's."""

    setup: Stage = Stage(max_size=3, repeats=7)


class AliasedModel(BaseModel):
    """A model with an aliased field."""

    model_config = ConfigDict(populate_by_name=True)
    max_size: int = Field(1, alias='maxSize')


class HoldsAliased(BaseModel):
    """A model that nests a model with an alias."""

    stages: List[AliasedModel] = Field(default_factory=list)


class FlagClash(BaseModel):
    """A boolean whose off flag is another field's flag."""

    verbose: bool = False
    no_verbose: str = ''


class Tricky(BaseModel):
    """Fields a re-dump of the model would not carry intact."""

    model_config = ConfigDict(extra='forbid')
    count: int = 1
    secret: int = Field(0, exclude=True)
    ceiling: float = 1.0

    @computed_field
    @property
    def double(self) -> int:
        """Twice the count."""
        return 2 * self.count


class EchoService:
    """An XML-RPC service that echoes what it gets back as JSON."""

    @pydantic_method
    def echo_tricky(self, params: Tricky):
        """The Tricky it got, with the excluded field."""
        return json.dumps({'count': params.count, 'secret': params.secret,
                           'ceiling': str(params.ceiling)})


class JobService:
    """An XML-RPC service with one pydantic command."""

    @pydantic_method
    def run_job(self, params: Job):
        """Run a job (here: describe it)."""
        return f'ran {params.name} x{params.rounds}'

    @pydantic_method
    def fail_job(self, params: Job):
        """A job that always fails."""
        raise RuntimeError(f'no luck with {params.name}')


def _job_file(tmp_path, data):
    """Write *data* as JSON to a file in *tmp_path*; its path."""
    path = tmp_path / 'job.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return str(path)


JOB_DATA = {
    'name': 'demo', 'rounds': 3, 'verbose': True,
    'waits': [1.0, 2.0, 3.0],
    'stops': [{'miles': 100, 'month': 'Jan'}],
    'setup': {'max_size': 4, 'step': 0.05, 'dry_run': True},
}


def _invoke(model, args, callback=None, name='job', **kwargs):
    """Run a command built from *model* with *args*; (result, cap)."""
    cap = callback if callback is not None else Capture()
    cmd = click_command_from_model(model, callback=cap, name=name)
    return CliRunner().invoke(cmd, args, **kwargs), cap


class TestParamsFile:
    """Tests for --params FILE and how options override it."""

    def test_file_alone(self, tmp_path):
        """mycli --params job.json"""
        result, cap = _invoke(Job, ['--params',
                                    _job_file(tmp_path, JOB_DATA)])
        assert result.exit_code == 0, result.output
        assert cap.instance == Job.model_validate(JOB_DATA)

    def test_typed_options_replace_file_values(self, tmp_path):
        """mycli --params job.json --rounds 5 --setup.max-size 9"""
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rounds', '5', '--setup.max-size', '9'])
        assert result.exit_code == 0, result.output
        assert cap.instance.rounds == 5
        assert cap.instance.setup.max_size == 9
        assert cap.instance.setup.step == 0.05     # kept from the file

    def test_defaults_do_not_replace_file_values(self, tmp_path):
        """mycli --params job.json --name other

        Options left out (whose defaults differ from the file)
        keep the file's values.
        """
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--name', 'other'])
        assert result.exit_code == 0, result.output
        assert cap.instance.name == 'other'
        assert cap.instance.rounds == 3
        assert cap.instance.setup.dry_run is True
        assert cap.instance.waits == [1.0, 2.0, 3.0]

    def test_off_flag_overrides_true_in_file(self, tmp_path):
        """mycli --params job.json --setup.no-dry-run --no-verbose"""
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--setup.no-dry-run', '--no-verbose'])
        assert result.exit_code == 0, result.output
        assert cap.instance.setup.dry_run is False
        assert cap.instance.verbose is False

    def test_list_options_replace_whole_lists(self, tmp_path):
        """mycli --params job.json --waits 7 \\
               --stops '{"miles":5,"month":"Feb"}'
        """
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--waits', '7',
            '--stops', json.dumps({'miles': 5, 'month': 'Feb'})])
        assert result.exit_code == 0, result.output
        assert cap.instance.waits == [7.0]
        assert [stop.miles for stop in cap.instance.stops] == [5.0]

    def test_options_fill_fields_missing_from_file(self, tmp_path):
        """mycli --params partial.json --setup.max-size 2

        Required fields may come from the file, the options or
        both.
        """
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, {'name': 'x'}),
            '--setup.max-size', '2'])
        assert result.exit_code == 0, result.output
        assert (cap.instance.name, cap.instance.setup.max_size) == (
            'x', 2)

    def test_still_missing_field_is_an_error(self, tmp_path):
        """mycli --params partial.json

        The file lacks setup.max_size and no option gives it.
        """
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, {'name': 'x',
                                             'setup': {}})])
        assert result.exit_code != 0
        assert 'setup.max_size' in result.output
        assert cap.instance is None

    def test_file_from_stdin(self):
        """cat job.json | mycli --params - --rounds 2"""
        result, cap = _invoke(Job, ['--params', '-', '--rounds', '2'],
                              input=json.dumps(JOB_DATA))
        assert result.exit_code == 0, result.output
        assert (cap.instance.name, cap.instance.rounds) == ('demo', 2)

    def test_file_that_is_not_json(self, tmp_path):
        """mycli --params broken.json"""
        path = tmp_path / 'broken.json'
        path.write_text('{"name": ', encoding='utf-8')
        result, cap = _invoke(Job, ['--params', str(path)])
        assert result.exit_code != 0
        assert 'is not JSON' in result.output
        assert cap.instance is None

    def test_file_that_holds_a_list(self, tmp_path):
        """mycli --params list.json"""
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, [JOB_DATA])])
        assert result.exit_code != 0
        assert 'holds a JSON list' in result.output
        assert cap.instance is None

    def test_missing_file(self, tmp_path):
        """mycli --params nowhere.json"""
        result, _cap = _invoke(Job, [
            '--params', str(tmp_path / 'nowhere.json')])
        assert result.exit_code != 0
        assert 'nowhere.json' in result.output


class TestShowParams:
    """Tests for --show-params."""

    def test_prints_merged_values_and_does_not_run(self, tmp_path):
        """mycli --params job.json --rounds 8 --show-params"""
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rounds', '8', '--show-params'])
        assert result.exit_code == 0, result.output
        assert cap.instance is None
        shown = json.loads(result.output)
        assert shown['rounds'] == 8
        assert shown['setup'] == JOB_DATA['setup']

    def test_output_loads_back_as_params(self, tmp_path):
        """mycli --params job.json --show-params > new.json
        mycli --params new.json
        """
        first, _cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--show-params'])
        path = tmp_path / 'new.json'
        path.write_text(first.output, encoding='utf-8')
        result, cap = _invoke(Job, ['--params', str(path)])
        assert result.exit_code == 0, result.output
        assert cap.instance == Job.model_validate(JOB_DATA)


class TestOptionKinds:
    """Tests for the on/off flags, scalar lists and defaults."""

    def test_scalar_list_default(self):
        """mycli --name a --setup.max-size 1

        Before 0.4.7 the default list reached pydantic as one
        newline-joined string and failed validation.
        """
        result, cap = _invoke(Job, ['--name', 'a',
                                    '--setup.max-size', '1'])
        assert result.exit_code == 0, result.output
        assert cap.instance.waits == [2.0, 4.0]

    def test_scalar_list_repeated(self):
        """mycli --name a --setup.max-size 1 --waits 1.5 --waits 3"""
        result, cap = _invoke(Job, [
            '--name', 'a', '--setup.max-size', '1',
            '--waits', '1.5', '--waits', '3'])
        assert result.exit_code == 0, result.output
        assert cap.instance.waits == [1.5, 3.0]

    def test_scalar_list_values_are_typed(self):
        """mycli --name a --setup.max-size 1 --waits soon"""
        result, cap = _invoke(Job, [
            '--name', 'a', '--setup.max-size', '1', '--waits', 'soon'])
        assert result.exit_code != 0
        assert cap.instance is None

    def test_optional_choice_left_out_stays_none(self):
        """mycli --name a --setup.max-size 1

        Before 0.4.7 a left-out Optional[Literal] got its first
        choice instead of its default None.
        """
        result, cap = _invoke(Job, ['--name', 'a',
                                    '--setup.max-size', '1'])
        assert result.exit_code == 0, result.output
        assert cap.instance.color is None

    def test_help_marks_required_fields(self):
        """mycli --help"""
        result, _cap = _invoke(Job, ['--help'])
        assert result.exit_code == 0
        assert '[required]' in result.output
        assert '--setup.no-dry-run' in result.output
        assert '--params' in result.output
        assert '--rpc-url' in result.output

    def test_bad_json_item_names_its_option(self):
        """mycli --origin X --stops '{not json'"""
        result, cap = _invoke(Route, ['--origin', 'X',
                                      '--stops', '{not json'])
        assert result.exit_code != 0
        assert '--stops' in result.output
        assert cap.instance is None

    def test_field_using_an_added_flag_is_refused(self):
        """A model with a ``params`` field cannot get --params."""
        with pytest.raises(ValueError, match='--params'):
            click_command_from_model(ClashingModel, name='clash')

    def test_two_fields_with_one_flag_are_refused(self):
        """``no_verbose`` clashes with the off flag of ``verbose``."""
        with pytest.raises(ValueError, match='--no-verbose'):
            click_command_from_model(FlagClash, name='clash')

    def test_aliases_are_refused(self):
        """A model with an aliased field is refused."""
        with pytest.raises(ValueError, match='alias'):
            click_command_from_model(AliasedModel, name='alias')

    def test_nested_aliases_are_refused(self):
        """A model nesting an aliased model is refused too."""
        with pytest.raises(ValueError, match='max_size'):
            click_command_from_model(HoldsAliased, name='alias')

    def test_override_keeps_a_nested_default(self):
        """mycli --setup.max-size 5

        ``setup`` defaults to Stage(max_size=3, repeats=7); overriding
        max_size keeps repeats=7 rather than Stage's own default 1.
        """
        result, cap = _invoke(WithDefaultStage, ['--setup.max-size', '5'])
        assert result.exit_code == 0, result.output
        assert cap.instance.setup == Stage(max_size=5, repeats=7)

    def test_file_value_beats_a_nested_default(self, tmp_path):
        """mycli --params f.json --setup.max-size 5

        When the file gives ``setup``, its values are the base.
        """
        result, cap = _invoke(WithDefaultStage, [
            '--params', _job_file(tmp_path, {'setup': {'repeats': 2}}),
            '--setup.max-size', '5'])
        assert result.exit_code == 0, result.output
        assert cap.instance.setup == Stage(max_size=5, repeats=2)


# ---------------------------------------------------------------
# --rpc-url: a real XML-RPC server in a thread
# ---------------------------------------------------------------

@pytest.fixture
def rpc_url():
    """URL of an XML-RPC server serving a JobService."""
    server = SimpleXMLRPCServer(('127.0.0.1', 0), allow_none=True,
                                logRequests=False)
    server.register_instance(JobService())
    thread = threading.Thread(target=server.serve_forever,
                              daemon=True)
    thread.start()
    host, port = server.server_address
    yield f'{host}:{port}'
    server.shutdown()
    server.server_close()


def _refused_url():
    """URL of a local port that nothing listens on."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return 'http://127.0.0.1:%d' % sock.getsockname()[1]


def _silent_server():
    """A listening socket that never answers, and its URL."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(1)
    return sock, 'http://127.0.0.1:%d' % sock.getsockname()[1]


class DropOnce:
    """A port that reads one XML-RPC request, then sends *answer*
    (nothing by default) and hangs up, counting what it got."""

    def __init__(self, answer=b''):
        """Listen on a free local port and serve in a thread."""
        self.answer = answer
        self.requests = 0
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(5)
        self.url = 'http://127.0.0.1:%d' % self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        """Read requests and hang up after each, until closed."""
        while True:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b''
                while b'</methodCall>' not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                self.requests += 1
                if self.answer:
                    conn.sendall(self.answer)

    def close(self):
        """Stop listening (the shutdown wakes the blocked accept)."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.thread.join(5)


def _interrupt(*_args, **_kwargs):
    """Stand-in for send_to_server: the user pressed Ctrl-C."""
    raise KeyboardInterrupt


class TestRpcUrl:
    """Tests for sending a command with --rpc-url."""

    def test_runs_on_server_not_here(self, rpc_url, tmp_path):
        """mycli run_job --params job.json --rounds 2 \\
               --rpc-url HOST:PORT
        """
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rounds', '2', '--rpc-url', rpc_url], name='run_job')
        assert result.exit_code == 0, result.output
        assert 'ran demo x2' in result.output
        assert cap.instance is None

    def test_works_without_a_local_callback(self, rpc_url, tmp_path):
        """A command with no callback can still go to a server."""
        cmd = click_command_from_model(Job, name='run_job')
        result = CliRunner().invoke(cmd, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rpc-url', f'http://{rpc_url}', '--rpc-timeout', '10'])
        assert result.exit_code == 0, result.output
        assert 'ran demo x3' in result.output

    def test_no_callback_and_no_url(self, tmp_path):
        """A command with no callback says to use --rpc-url."""
        cmd = click_command_from_model(Job, name='run_job')
        result = CliRunner().invoke(cmd, [
            '--params', _job_file(tmp_path, JOB_DATA)])
        assert result.exit_code != 0
        assert '--rpc-url' in result.output

    def test_validates_before_sending(self, rpc_url, tmp_path):
        """mycli run_job --params partial.json --rpc-url HOST:PORT

        An invalid job is refused here and never sent.
        """
        result, _cap = _invoke(Job, [
            '--params', _job_file(tmp_path, {'name': 'x',
                                             'setup': {}}),
            '--rpc-url', rpc_url], name='run_job')
        assert result.exit_code != 0
        assert 'setup.max_size' in result.output
        assert 'Sending' not in result.output

    def test_server_failure_is_reported(self, rpc_url, tmp_path):
        """mycli fail_job --params job.json --rpc-url HOST:PORT"""
        result, _cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rpc-url', rpc_url], name='fail_job')
        assert result.exit_code == 1
        assert 'reported a failure in fail_job' in result.output
        assert 'no luck with demo' in result.output

    def test_nothing_listening(self, tmp_path):
        """mycli run_job --params job.json --rpc-url DEAD:PORT"""
        result, _cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rpc-url', _refused_url()], name='run_job')
        assert result.exit_code == 1
        assert 'nothing was sent' in result.output

    def test_timeout_says_it_may_still_run(self, tmp_path):
        """mycli run_job ... --rpc-url SILENT:PORT --rpc-timeout 0.2"""
        sock, url = _silent_server()
        try:
            result, _cap = _invoke(Job, [
                '--params', _job_file(tmp_path, JOB_DATA),
                '--rpc-url', url, '--rpc-timeout', '0.2'],
                name='run_job')
        finally:
            sock.close()
        assert result.exit_code == 1
        assert 'within 0.2 s' in result.output
        assert 'may still be running' in result.output

    def test_ctrl_c_stops_only_the_wait(self, tmp_path, monkeypatch):
        """Ctrl-C while waiting says the command may still run."""
        monkeypatch.setattr(cli_builder, 'send_to_server', _interrupt)
        result, _cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rpc-url', 'h:1'], name='run_job')
        assert result.exit_code == 1
        assert 'Stopped waiting' in result.output
        assert 'may still be running on http://h:1' in result.output

    def test_command_without_a_name(self, tmp_path):
        """A command built with no name cannot be sent."""
        result, _cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rpc-url', 'h:1'], name=None)
        assert result.exit_code != 0
        assert 'no name' in result.output

    def test_empty_url_is_refused(self, tmp_path):
        """mycli run_job ... --rpc-url ''

        An empty URL (e.g. an unset variable) must not run the
        command here instead.
        """
        result, cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rpc-url', ' '], name='run_job')
        assert result.exit_code == 2
        assert 'is empty' in result.output
        assert cap.instance is None

    def test_sent_once_when_the_connection_drops(self):
        """A server that reads the request and hangs up gets it
        once, and the message says it may be running."""
        server = DropOnce()
        try:
            result, _cap = _invoke(Job, [
                '--name', 'n', '--setup.max-size', '1',
                '--rpc-url', server.url, '--rpc-timeout', '10'],
                name='run_job')
        finally:
            server.close()
        assert result.exit_code == 1
        assert 'may be running there' in result.output
        assert server.requests == 1

    def test_garbage_answer_is_reported(self):
        """A port that answers with something other than XML-RPC."""
        server = DropOnce(answer=b'SSH-2.0-OpenSSH_9.6\r\n')
        try:
            result, _cap = _invoke(Job, [
                '--name', 'n', '--setup.max-size', '1',
                '--rpc-url', server.url, '--rpc-timeout', '10'],
                name='run_job')
        finally:
            server.close()
        assert result.exit_code == 1
        assert 'may be running there' in result.output

    def test_server_gets_the_input_not_a_re_dump(self, tmp_path):
        """mycli echo_tricky --secret 5 --ceiling inf --rpc-url ...

        A re-dump of the model would drop the excluded field, add
        the computed one (which extra='forbid' refuses) and turn
        inf into null.
        """
        server = SimpleXMLRPCServer(('127.0.0.1', 0), allow_none=True,
                                    logRequests=False)
        server.register_instance(EchoService())
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        try:
            result, _cap = _invoke(Tricky, [
                '--count', '2', '--secret', '5', '--ceiling', 'inf',
                '--rpc-url', '%s:%d' % server.server_address],
                name='echo_tricky')
        finally:
            server.shutdown()
            server.server_close()
        assert result.exit_code == 0, result.output
        echoed = json.loads(result.output.splitlines()[-1])
        assert echoed == {'count': 2, 'secret': 5, 'ceiling': 'inf'}

    def test_timeout_must_be_positive(self, tmp_path):
        """mycli run_job ... --rpc-timeout 0"""
        result, _cap = _invoke(Job, [
            '--params', _job_file(tmp_path, JOB_DATA),
            '--rpc-url', 'h:1', '--rpc-timeout', '0'], name='run_job')
        assert result.exit_code == 2

    def test_group_from_cls_sends_to_server(self, rpc_url, tmp_path):
        """svc run_job --params job.json --rpc-url HOST:PORT

        The group's commands carry the method names, so a client
        built from the same class calls the server's methods.
        """
        group = click_group_from_cls(JobService, name='svc')
        result = CliRunner().invoke(group, [
            'run_job', '--params', _job_file(tmp_path, JOB_DATA),
            '--rpc-url', rpc_url])
        assert result.exit_code == 0, result.output
        assert 'ran demo x3' in result.output
