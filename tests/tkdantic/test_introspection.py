"""Tests for :mod:`ox_ui.tkdantic.introspection`.

These tests need no display: introspection is pure logic.
"""

import pathlib
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from ox_ui.tkdantic.introspection import (
    all_fields_have_defaults,
    introspect_field,
    introspect_model,
    resolve_default,
)


# ---------------------------------------------------------------
# Test models
# ---------------------------------------------------------------

class Stop(BaseModel):
    """One stop on a trip."""

    miles: float
    month: str = 'Dec'


class NoDocModel(BaseModel):
    value: int = 0


class Everything(BaseModel):
    """A model with one field of every supported kind."""

    name: str = Field(description='Traveler name.')
    count: int = 3
    speed: float = 1.5
    active: bool = False
    transport: Literal['train', 'bus'] = 'bus'
    note: Optional[str] = None
    out_file: pathlib.Path = Field(
        default=pathlib.Path('out.csv'),
        json_schema_extra={
            'mode': 'open',
            'filetypes': [('CSV', '*.csv')],
            'defaultextension': '.csv',
        },
    )
    stop: Stop
    stops: List[Stop] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=lambda: ['a', 'b'])
    sizes: List[int] = Field(default_factory=list)


class AllDefaults(BaseModel):
    """Every field has a default or a default factory."""

    a: int = 1
    b: List[int] = Field(default_factory=list)


class OneRequired(BaseModel):
    """Field ``b`` is required."""

    a: int = 1
    b: str


def _spec(model, name):
    """Return the spec for field *name* of *model*."""
    by_name = {s['name']: s for s in introspect_model(model)}
    return by_name[name]


# ---------------------------------------------------------------
# resolve_default
# ---------------------------------------------------------------

class TestResolveDefault:
    """Tests for :func:`resolve_default`."""

    def test_static_default(self):
        """A static default is returned as is."""
        assert resolve_default(FieldInfo(default=7)) == 7

    def test_default_factory(self):
        """A default factory is called to produce the default."""
        info = FieldInfo(default_factory=list)
        assert resolve_default(info) == []

    def test_no_default(self):
        """A required field resolves to None."""
        assert resolve_default(FieldInfo()) is None


# ---------------------------------------------------------------
# introspect_model / introspect_field
# ---------------------------------------------------------------

class TestScalarSpecs:
    """Specs for str, int, float, bool and Literal fields."""

    def test_field_sequence_preserved(self):
        """Specs come back in the model's field sequence."""
        names = [s['name'] for s in introspect_model(Everything)]
        assert names == list(Everything.model_fields)

    def test_str_with_help(self):
        """A str field keeps its description as help text."""
        spec = _spec(Everything, 'name')
        assert spec['gui_type'] == 'str'
        assert spec['help'] == 'Traveler name.'
        assert spec['default'] == ''
        assert spec['nullable'] is False

    def test_int_and_float_defaults_are_strings(self):
        """Numeric defaults are converted to strings for widgets."""
        assert _spec(Everything, 'count')['default'] == '3'
        assert _spec(Everything, 'count')['gui_type'] == 'int'
        assert _spec(Everything, 'speed')['default'] == '1.5'
        assert _spec(Everything, 'speed')['gui_type'] == 'float'

    def test_bool_keeps_python_default(self):
        """A bool field keeps its bool default (not a string)."""
        spec = _spec(Everything, 'active')
        assert spec['gui_type'] == 'bool'
        assert spec['default'] is False

    def test_bool_without_default_is_true(self):
        """A required bool field gets True as its widget default."""
        spec = introspect_field('flag', bool, FieldInfo())
        assert spec['default'] is True

    def test_literal_choice(self):
        """A Literal field becomes a choice with string choices."""
        spec = _spec(Everything, 'transport')
        assert spec['gui_type'] == 'choice'
        assert spec['choices'] == ['train', 'bus']
        assert spec['default'] == 'bus'

    def test_literal_without_default_uses_first(self):
        """A required Literal field defaults to its first choice."""
        spec = introspect_field(
            'n', Literal[1, 2], FieldInfo(),
        )
        assert spec['choices'] == ['1', '2']
        assert spec['default'] == '1'

    def test_optional_str_is_nullable(self):
        """Optional[str] with default None is a nullable str."""
        spec = _spec(Everything, 'note')
        assert spec['gui_type'] == 'str'
        assert spec['nullable'] is True
        assert spec['default'] == ''

    def test_pipe_union_optional(self):
        """``int | None`` is unwrapped like Optional[int]."""
        spec = introspect_field(
            'x', int | None, FieldInfo(default=None),
        )
        assert spec['gui_type'] == 'int'
        assert spec['nullable'] is True

    def test_explicit_none_default_is_nullable(self):
        """A plain int with default None is treated as nullable."""
        spec = introspect_field('x', int, FieldInfo(default=None))
        assert spec['nullable'] is True

    def test_multi_type_union_falls_back_to_str(self):
        """A Union of two real types falls back to a str field."""
        spec = introspect_field(
            'x', Union[int, str], FieldInfo(default=5),
        )
        assert spec['gui_type'] == 'str'
        assert spec['default'] == '5'
        assert spec['nullable'] is False

    def test_unknown_type_falls_back_to_str(self):
        """An unsupported annotation is shown as a str field."""
        spec = introspect_field('x', dict, FieldInfo(default={}))
        assert spec['gui_type'] == 'str'


class TestFilepathSpecs:
    """Specs for ``pathlib.Path`` fields."""

    def test_extras_used(self):
        """json_schema_extra sets mode, filetypes and extension."""
        spec = _spec(Everything, 'out_file')
        assert spec['gui_type'] == 'filepath'
        assert spec['default'] == 'out.csv'
        assert spec['mode'] == 'open'
        assert spec['filetypes'] == [('CSV', '*.csv')]
        assert spec['defaultextension'] == '.csv'

    def test_defaults_without_extras(self):
        """Without extras the dialog is a save dialog for any file."""
        spec = introspect_field(
            'p', Optional[pathlib.PurePath], FieldInfo(default=None),
        )
        assert spec['gui_type'] == 'filepath'
        assert spec['nullable'] is True
        assert spec['default'] == ''
        assert spec['mode'] == 'save'
        assert spec['filetypes'] == [('All files', '*.*')]
        assert spec['defaultextension'] == ''


class TestNestedSpecs:
    """Specs for sub-models and lists."""

    def test_sub_model(self):
        """A BaseModel field becomes a model spec with its doc."""
        spec = _spec(Everything, 'stop')
        assert spec['gui_type'] == 'model'
        assert spec['model'] is Stop
        assert spec['help'] == 'One stop on a trip.'

    def test_sub_model_without_doc(self):
        """A sub-model without a docstring gets empty help."""
        spec = introspect_field('m', NoDocModel, FieldInfo())
        assert spec['help'] == ''

    def test_model_list(self):
        """List[BaseModel] becomes a model_list spec."""
        spec = _spec(Everything, 'stops')
        assert spec['gui_type'] == 'model_list'
        assert spec['model'] is Stop
        assert spec['help'] == 'One stop on a trip.'

    def test_scalar_list_default_lines(self):
        """List[str] defaults are joined with newlines."""
        spec = _spec(Everything, 'tags')
        assert spec['gui_type'] == 'scalar_list'
        assert spec['inner_type'] == 'str'
        assert spec['default'] == 'a\nb'

    def test_int_scalar_list(self):
        """List[int] records int as its inner type."""
        spec = _spec(Everything, 'sizes')
        assert spec['inner_type'] == 'int'
        assert spec['default'] == ''

    def test_unknown_inner_type_is_str(self):
        """A list of an unsupported scalar uses str elements."""
        spec = introspect_field(
            'x', List[bytes], FieldInfo(default=None),
        )
        assert spec['gui_type'] == 'scalar_list'
        assert spec['inner_type'] == 'str'
        assert spec['nullable'] is True

    def test_untyped_list_falls_back_to_str(self):
        """A bare ``List`` annotation falls back to a str field."""
        spec = introspect_field('x', List, FieldInfo(default=None))
        assert spec['gui_type'] == 'str'


class TestAllFieldsHaveDefaults:
    """Tests for :func:`all_fields_have_defaults`."""

    def test_all_defaults(self):
        """Defaults and default factories both count."""
        assert all_fields_have_defaults(AllDefaults)

    def test_required_field(self):
        """One required field makes the result False."""
        assert not all_fields_have_defaults(OneRequired)
