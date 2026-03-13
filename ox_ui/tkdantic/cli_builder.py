"""Build Click CLI command groups from pydantic models.

This module mirrors the GUI-building capabilities of
:mod:`builder` but targets `Click <https://click.palletsprojects.com>`_
command-line interfaces.

Given a list of :class:`Command` objects (or a class decorated
with ``@pydantic_method``), it produces a :class:`click.Group`
with one subcommand per command whose options are derived from
the pydantic model's fields.

Nested ``BaseModel`` fields are flattened into dotted CLI
options (e.g. ``--address.city``).  ``List[BaseModel]`` fields
accept repeated JSON strings via ``--field '{...}'``.

Example usage::

    group = click_group_from_commands(commands, name='mycli')
    group()  # hand off to Click
"""

import json
import logging

import click
from pydantic import BaseModel, ValidationError

from ox_ui.tkdantic.introspection import introspect_model
from ox_ui.tkdantic.command import Command
from ox_ui.tkdantic.inspector import commands_for_cls

LOGGER = logging.getLogger(__name__)

# Internal separator used in Python parameter names to encode
# nesting (e.g. ``address__city``).  The CLI uses dots instead
# (``--address.city``).
_SEP = '__'


# -----------------------------------------------------------
# Helpers: field-spec → Click option mapping
# -----------------------------------------------------------

_GUI_TO_CLICK_TYPE = {
    'int': int,
    'float': float,
    'str': str,
}


def _click_type_for(gui_type):
    """Return the Click parameter type for a *gui_type* string.

    Falls back to ``str`` for unrecognised types.
    """
    return _GUI_TO_CLICK_TYPE.get(gui_type, str)


def _make_flag_and_param(prefix, name):
    """Build a CLI flag and a Python-safe parameter name.

    >>> _make_flag_and_param('', 'zip_code')
    ('--zip-code', 'zip_code')
    >>> _make_flag_and_param('address', 'zip_code')
    ('--address.zip-code', 'address__zip_code')
    """
    clean = name.replace('_', '-')
    if prefix:
        dotted = prefix.replace(_SEP, '.')
        dotted = dotted.replace('_', '-')
        flag = f'--{dotted}.{clean}'
        param = f'{prefix}{_SEP}{name}'
    else:
        flag = f'--{clean}'
        param = name
    return flag, param


def _is_required(spec):
    """Return ``True`` if *spec* represents a required field.

    A field is required when it has no usable default and is
    not nullable.  Booleans always have a default.
    """
    if spec['gui_type'] == 'bool':
        return False
    if spec.get('nullable', False):
        return False
    default = spec.get('default', '')
    return default == '' or default is None


def _cast_default(raw_default, click_type):
    """Cast the string *raw_default* to *click_type*.

    Returns ``None`` on failure or when the raw value is
    the empty string (which means "no default" in the spec
    format produced by :func:`introspect_model`).
    """
    if raw_default == '' or raw_default is None:
        return None
    try:
        return click_type(raw_default)
    except (ValueError, TypeError):
        return None


def _scalar_option_kwargs(spec, prefix=''):
    """Build ``click.option`` arguments for a scalar spec.

    Returns ``(cli_flag, param_name, kwargs_dict)``.
    """
    name = spec['name']
    flag, param = _make_flag_and_param(prefix, name)
    gui_type = spec['gui_type']
    kwargs = {}

    if gui_type == 'bool':
        kwargs['is_flag'] = True
        kwargs['default'] = spec.get('default', False)

    elif gui_type == 'choice':
        kwargs['type'] = click.Choice(spec['choices'])
        kwargs['default'] = spec.get('default')

    else:
        ctype = _click_type_for(gui_type)
        kwargs['type'] = ctype
        kwargs['default'] = _cast_default(
            spec.get('default', ''), ctype,
        )

    kwargs['required'] = _is_required(spec)

    help_text = spec.get('help', '')
    if help_text:
        kwargs['help'] = help_text

    return flag, param, kwargs


def _model_list_help(model_class):
    """Build help text for a ``List[BaseModel]`` JSON option.

    Lists the sub-model's field names so the user knows
    which keys to include in the JSON object.
    """
    fields = list(model_class.model_fields.keys())
    names = ', '.join(fields)
    return (
        f'JSON object with keys: {names}. '
        f'Repeat for multiple items.'
    )


# -----------------------------------------------------------
# Recursive spec flattener
# -----------------------------------------------------------

def _flatten_to_options(specs, prefix=''):
    """Yield option descriptors for a list of field specs.

    Each yielded item is ``(flag, param, kwargs, meta)``:

    *   *flag* / *param* – the CLI flag and Python name.
    *   *kwargs* – ready to unpack into ``click.option``.
    *   *meta* – dict; contains ``model_class`` for
        ``model_list`` fields (empty dict otherwise).

    Nested ``model`` specs are recursively flattened with
    dotted prefixes.  ``model_list`` specs become repeated
    JSON-string options.
    """
    for spec in specs:
        gui_type = spec['gui_type']
        name = spec['name']

        if gui_type == 'model':
            child_prefix = (
                f'{prefix}{_SEP}{name}' if prefix else name
            )
            sub_specs = introspect_model(spec['model'])
            yield from _flatten_to_options(
                sub_specs, child_prefix,
            )

        elif gui_type == 'model_list':
            flag, param = _make_flag_and_param(prefix, name)
            yield (flag, param, {
                'type': str,
                'multiple': True,
                'help': _model_list_help(spec['model']),
            }, {'model_class': spec['model']})

        else:
            flag, param, kwargs = _scalar_option_kwargs(
                spec, prefix,
            )
            yield (flag, param, kwargs, {})


# -----------------------------------------------------------
# Kwargs reassembly (flat → nested dict)
# -----------------------------------------------------------

def _reassemble_kwargs(flat_kwargs, model_list_params):
    """Rebuild a nested dict from flattened Click kwargs.

    *flat_kwargs* is the ``**kwargs`` dict that Click passes
    to the command callback.  Keys containing ``__`` are split
    and nested.  Keys listed in *model_list_params* have their
    tuple-of-JSON-strings parsed into lists of dicts.

    ``None`` values (unset optional options) are omitted so
    that pydantic can apply its own defaults.
    """
    result = {}
    for key, value in flat_kwargs.items():
        # Parse JSON for model_list fields
        if key in model_list_params:
            value = [json.loads(v) for v in value]
            if not value:
                continue

        # Skip None so pydantic applies its own defaults
        if value is None:
            continue

        # Walk the nesting path
        parts = key.split(_SEP)
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    return result


# -----------------------------------------------------------
# Dispatch hook (callback now, RPC later)
# -----------------------------------------------------------

def _dispatch(command, instance):
    """Call the command's callback with *instance*.

    This is the single dispatch point that can be extended
    to support XML-RPC or other transports in the future.
    """
    if command.callback is not None:
        return command.callback(instance)
    raise click.UsageError(
        f'No callback registered for "{command.name}".  '
        f'(RPC dispatch is not yet implemented.)'
    )


# -----------------------------------------------------------
# Public API: single command
# -----------------------------------------------------------

def click_command_from_model(
    model_class, callback=None, name=None, help_text=None,
):
    """Build a :class:`click.Command` from a pydantic model.

    Each model field becomes a CLI option:

    *   Scalar fields (``str``, ``int``, ``float``, ``bool``,
        ``Literal[...]``) map to typed Click options.
    *   Nested ``BaseModel`` fields are flattened into dotted
        options (``--address.city``).
    *   ``List[BaseModel]`` fields accept repeated JSON
        (``--legs '{...}' --legs '{...}'``).

    The *callback* receives a validated model instance and its
    return value (if not ``None``) is echoed to stdout.
    """
    specs = introspect_model(model_class)
    options = list(_flatten_to_options(specs))

    model_list_params = {
        param: meta['model_class']
        for _, param, _, meta in options
        if 'model_class' in meta
    }

    def invoke(**kwargs):
        """Click callback: reassemble, validate, dispatch."""
        raw = _reassemble_kwargs(kwargs, model_list_params)
        try:
            instance = model_class.model_validate(raw)
        except ValidationError as exc:
            raise click.UsageError(str(exc)) from exc
        if callback is None:
            raise click.UsageError(
                'No callback registered.  '
                '(RPC dispatch is not yet implemented.)'
            )
        result = callback(instance)
        if result is not None:
            click.echo(result)

    # Apply option decorators (reversed for natural order)
    for flag, param, kw, _meta in reversed(options):
        invoke = click.option(flag, param, **kw)(invoke)

    return click.command(
        name=name,
        help=help_text or model_class.__doc__ or '',
    )(invoke)


# -----------------------------------------------------------
# Public API: command group from Command list
# -----------------------------------------------------------

def click_group_from_commands(commands, name=None):
    """Build a :class:`click.Group` from :class:`Command` objects.

    Each ``Command`` with at least one pydantic parameter
    becomes a subcommand.  Commands without parameters that
    have a callback are added as simple no-argument commands.
    """
    group = click.Group(name=name)
    for cmd in commands:
        if cmd.parameters:
            sub = _command_with_params(cmd)
        elif cmd.callback is not None:
            sub = _command_without_params(cmd)
        else:
            continue
        group.add_command(sub)
    return group


def _command_with_params(cmd):
    """Build a click subcommand for a Command with parameters."""
    if len(cmd.parameters) != 1:
        raise ValueError(
            f'Command "{cmd.name}" has {len(cmd.parameters)}'
            f' parameters; only 1 is supported.'
        )
    return click_command_from_model(
        model_class=cmd.parameters[0],
        callback=cmd.callback,
        name=cmd.name,
        help_text=cmd.description,
    )


def _command_without_params(cmd):
    """Build a click subcommand for a no-parameter Command."""
    @click.command(
        name=cmd.name, help=cmd.description,
    )
    def invoke():
        """Invoke a parameter-less command."""
        result = cmd.callback()
        if result is not None:
            click.echo(result)
    return invoke


# -----------------------------------------------------------
# Public API: convenience for @pydantic_method classes
# -----------------------------------------------------------

def click_group_from_cls(klass, name=None, instance=None):
    """Build a :class:`click.Group` by inspecting *klass*.

    Discovers methods decorated with ``@pydantic_method`` via
    :func:`commands_for_cls`, then binds each method to a
    live *instance* so they can be invoked from the CLI.

    If *instance* is ``None`` (the default), *klass* is
    instantiated with no arguments.  Pass a pre-built
    instance when the constructor requires parameters.

    Example::

        class MyService:
            @pydantic_method
            def place_order(self, params: OrderModel):
                '''Place a new order.'''
                ...

        cli = click_group_from_cls(MyService, name='svc')
        cli()
    """
    commands = commands_for_cls(klass)
    if instance is None:
        instance = klass()
    commands = _bind_commands(commands, instance)
    return click_group_from_commands(commands, name=name)


def _bind_commands(commands, instance):
    """Bind discovered commands to a live *instance*.

    For each :class:`Command` whose ``callback`` is ``None``,
    look up the corresponding bound method on *instance* and
    attach it.  Returns a new list; the originals are not
    mutated.
    """
    bound = []
    for cmd in commands:
        if cmd.callback is not None:
            bound.append(cmd)
            continue
        method = getattr(instance, cmd.name, None)
        if method is None:
            bound.append(cmd)
            continue
        bound.append(cmd.model_copy(
            update={'callback': method},
        ))
    return bound
