"""Tests for :mod:`ox_ui.tkdantic.cli_builder`.

Each test method has a docstring showing the equivalent
command line being exercised.
"""

import json
from typing import List, Literal, Optional

import click
from click.testing import CliRunner
from pydantic import BaseModel, Field
import pytest

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

class SimpleOrder(BaseModel):
    """A simple order with basic scalar fields."""

    customer: str
    quantity: int
    price: float = 9.99


class Address(BaseModel):
    """Mailing address."""

    street: str
    city: str
    zip_code: str = '00000'


class OrderWithAddress(BaseModel):
    """An order containing a nested address sub-model."""

    customer: str
    address: Address


class Leg(BaseModel):
    """One leg of a multi-leg trade."""

    strike: float
    expiry: str


class Trade(BaseModel):
    """A multi-leg options trade."""

    symbol: str
    legs: List[Leg]


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
    legs: List[Leg]


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
        """--customer → customer"""
        flag, param = _make_flag_and_param('', 'customer')
        assert flag == '--customer'
        assert param == 'customer'

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
            'legs': (
                '{"strike": 100, "expiry": "Jan"}',
                '{"strike": 200, "expiry": "Feb"}',
            ),
        }
        result = _reassemble_kwargs(raw, {'legs': object})
        assert len(result['legs']) == 2
        assert result['legs'][0]['strike'] == 100


# ---------------------------------------------------------------
# Integration tests: click_command_from_model
# ---------------------------------------------------------------

class TestSimpleModel:
    """Tests for a flat model with str, int, float fields."""

    def test_all_options_provided(self):
        """mycli --customer Alice --quantity 5 --price 12.50"""
        cap = Capture()
        cmd = click_command_from_model(
            SimpleOrder, callback=cap, name='order',
        )
        result = CliRunner().invoke(cmd, [
            '--customer', 'Alice',
            '--quantity', '5',
            '--price', '12.50',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.customer == 'Alice'
        assert cap.instance.quantity == 5
        assert cap.instance.price == 12.50

    def test_default_value_used(self):
        """mycli --customer Bob --quantity 3

        The --price option is omitted; default 9.99 applies.
        """
        cap = Capture()
        cmd = click_command_from_model(
            SimpleOrder, callback=cap, name='order',
        )
        result = CliRunner().invoke(cmd, [
            '--customer', 'Bob',
            '--quantity', '3',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.price == 9.99

    def test_missing_required_option(self):
        """mycli --customer Alice

        Missing --quantity should produce an error.
        """
        cap = Capture()
        cmd = click_command_from_model(
            SimpleOrder, callback=cap, name='order',
        )
        result = CliRunner().invoke(cmd, [
            '--customer', 'Alice',
        ])
        assert result.exit_code != 0
        assert 'quantity' in result.output.lower()

    def test_callback_return_echoed(self):
        """mycli --customer X --quantity 1

        The callback's return value is echoed to stdout.
        """
        cap = Capture()
        cmd = click_command_from_model(
            SimpleOrder, callback=cap, name='order',
        )
        result = CliRunner().invoke(cmd, [
            '--customer', 'X',
            '--quantity', '1',
        ])
        assert 'OK' in result.output


class TestNestedModel:
    """Tests for a model with a nested BaseModel field."""

    def test_dotted_options(self):
        """mycli --customer Eve --address.street '123 Main' \\
               --address.city Boston
        """
        cap = Capture()
        cmd = click_command_from_model(
            OrderWithAddress, callback=cap, name='order',
        )
        result = CliRunner().invoke(cmd, [
            '--customer', 'Eve',
            '--address.street', '123 Main St',
            '--address.city', 'Boston',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.address.street == '123 Main St'
        assert cap.instance.address.city == 'Boston'

    def test_nested_default_applied(self):
        """mycli --customer Eve --address.street '123 Main' \\
               --address.city Boston

        The zip_code sub-field defaults to '00000'.
        """
        cap = Capture()
        cmd = click_command_from_model(
            OrderWithAddress, callback=cap, name='order',
        )
        result = CliRunner().invoke(cmd, [
            '--customer', 'Eve',
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
        """mycli --symbol AAPL \\
               --legs '{"strike":100,"expiry":"Jan"}' \\
               --legs '{"strike":200,"expiry":"Jun"}'
        """
        cap = Capture()
        cmd = click_command_from_model(
            Trade, callback=cap, name='trade',
        )
        leg1 = json.dumps({'strike': 100, 'expiry': 'Jan'})
        leg2 = json.dumps({'strike': 200, 'expiry': 'Jun'})
        result = CliRunner().invoke(cmd, [
            '--symbol', 'AAPL',
            '--legs', leg1,
            '--legs', leg2,
        ])
        assert result.exit_code == 0, result.output
        assert len(cap.instance.legs) == 2
        assert cap.instance.legs[0].strike == 100.0
        assert cap.instance.legs[1].expiry == 'Jun'

    def test_single_leg(self):
        """mycli --symbol TSLA \\
               --legs '{"strike":50,"expiry":"Mar"}'
        """
        cap = Capture()
        cmd = click_command_from_model(
            Trade, callback=cap, name='trade',
        )
        leg = json.dumps({'strike': 50, 'expiry': 'Mar'})
        result = CliRunner().invoke(cmd, [
            '--symbol', 'TSLA', '--legs', leg,
        ])
        assert result.exit_code == 0, result.output
        assert len(cap.instance.legs) == 1


class TestMixedModelAndList:
    """Tests for a model with both nested model and list."""

    def test_mixed(self):
        """mycli --title 'My Trade' \\
               --address.street '1 Wall St' \\
               --address.city NYC \\
               --legs '{"strike":100,"expiry":"Jan"}'
        """
        cap = Capture()
        cmd = click_command_from_model(
            MixedModel, callback=cap, name='mixed',
        )
        leg = json.dumps({'strike': 100, 'expiry': 'Jan'})
        result = CliRunner().invoke(cmd, [
            '--title', 'My Trade',
            '--address.street', '1 Wall St',
            '--address.city', 'NYC',
            '--legs', leg,
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.title == 'My Trade'
        assert cap.instance.address.street == '1 Wall St'
        assert cap.instance.legs[0].strike == 100.0


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
        """mycli --customer Alice --quantity not-a-number

        Click catches the type error before pydantic.
        """
        cap = Capture()
        cmd = click_command_from_model(
            SimpleOrder, callback=cap, name='order',
        )
        result = CliRunner().invoke(cmd, [
            '--customer', 'Alice',
            '--quantity', 'not-a-number',
        ])
        assert result.exit_code != 0


class TestHelpOutput:
    """Tests that --help is generated correctly."""

    def test_help_shows_options(self):
        """mycli --help

        Should list all options with their types.
        """
        cmd = click_command_from_model(
            SimpleOrder, name='order',
        )
        result = CliRunner().invoke(cmd, ['--help'])
        assert result.exit_code == 0
        assert '--customer' in result.output
        assert '--quantity' in result.output
        assert '--price' in result.output

    def test_help_shows_dotted_options(self):
        """mycli --help

        Nested model fields appear as dotted options.
        """
        cmd = click_command_from_model(
            OrderWithAddress, name='order',
        )
        result = CliRunner().invoke(cmd, ['--help'])
        assert '--address.street' in result.output
        assert '--address.city' in result.output

    def test_help_shows_model_list_hint(self):
        """mycli --help

        List[BaseModel] options should mention JSON keys.
        """
        cmd = click_command_from_model(
            Trade, name='trade',
        )
        result = CliRunner().invoke(cmd, ['--help'])
        assert 'strike' in result.output
        assert 'expiry' in result.output


# ---------------------------------------------------------------
# Integration tests: click_group_from_commands
# ---------------------------------------------------------------

class TestGroupFromCommands:
    """Tests for building a click.Group from Command objects."""

    def _make_group(self, capture):
        """Build a two-command group for testing."""
        commands = [
            Command(
                title='Place Order',
                parameters=[SimpleOrder],
                description='Place a new order.',
                callback=capture,
            ),
            Command(
                title='Show Trade',
                parameters=[Trade],
                description='Display a trade.',
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
        assert 'place_order' in result.output
        assert 'show_trade' in result.output

    def test_invoke_subcommand(self):
        """cli place_order --customer A --quantity 1"""
        cap = Capture()
        group = self._make_group(cap)
        result = CliRunner().invoke(group, [
            'place_order',
            '--customer', 'A',
            '--quantity', '1',
        ])
        assert result.exit_code == 0, result.output
        assert cap.instance.customer == 'A'


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
            def place_order(self, params: SimpleOrder):
                """Place a new order."""
                return f'Placed for {params.customer}'

        group = click_group_from_cls(MyService, name='svc')
        result = CliRunner().invoke(group, ['--help'])
        assert result.exit_code == 0
        assert 'place_order' in result.output

    def test_invoke_discovered(self):
        """svc place_order --customer Alice --quantity 2

        A method discovered via @pydantic_method is bound
        to an instance and callable through the generated CLI.
        """

        class MyService:
            """Example service."""

            @pydantic_method
            def place_order(self, params: SimpleOrder):
                """Place a new order."""
                return f'Placed for {params.customer}'

        group = click_group_from_cls(MyService, name='svc')
        result = CliRunner().invoke(group, [
            'place_order',
            '--customer', 'Alice',
            '--quantity', '2',
        ])
        assert result.exit_code == 0, result.output
        assert 'Placed for Alice' in result.output

    def test_with_prebuilt_instance(self):
        """svc place_order --customer Bob --quantity 1

        A pre-built instance can be passed explicitly when
        the constructor requires arguments.
        """

        class MyService:
            """Service needing init args."""

            def __init__(self, prefix):
                self.prefix = prefix

            @pydantic_method
            def place_order(self, params: SimpleOrder):
                """Place a new order."""
                return (
                    f'{self.prefix}: '
                    f'Placed for {params.customer}'
                )

        svc = MyService(prefix='TEST')
        group = click_group_from_cls(
            MyService, name='svc', instance=svc,
        )
        result = CliRunner().invoke(group, [
            'place_order',
            '--customer', 'Bob',
            '--quantity', '1',
        ])
        assert result.exit_code == 0, result.output
        assert 'TEST: Placed for Bob' in result.output
