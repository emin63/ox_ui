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
accept repeated JSON strings via ``--field '{...}'``, and
``List[scalar]`` fields accept repeated values.  Boolean fields
get an on and an off flag (``--verbose/--no-verbose``).

Every command also gets these options:

*   ``--params FILE`` starts from the values in a JSON file, such
    as one saved from the GUI.  Options typed on the command line
    replace the file's values; a list option replaces the whole
    list.
*   ``--show-params`` prints the final parameters as JSON and
    exits without running the command.
*   ``--rpc-url URL`` sends the command to an XML-RPC server
    (such as one serving the same class) instead of running it
    here, and waits for its result.  ``--rpc-timeout SECS`` sets
    how long to wait.  The request is sent once: it is never
    retried, since the command may already be running.

The values are checked here first, from the same JSON that a
server would get, so the server sees exactly what was checked.
Models whose fields use aliases are refused (see
:func:`_check_no_aliases`).

Example usage::

    group = click_group_from_commands(commands, name='mycli')
    group()  # hand off to Click

    # mycli plan_trip --params trip.json --seats 5 \\
    #     --rpc-url http://127.0.0.1:8765
"""

import http.client
import json
import logging
import xml.parsers.expat
import xmlrpc.client

import click
from click.core import ParameterSource
from pydantic import BaseModel, ValidationError

from ox_ui.core.simple_rpc_client import TimeoutTransport
from ox_ui.tkdantic.introspection import (
    _unwrap_optional,
    introspect_model,
    resolve_default,
)
from ox_ui.tkdantic.command import Command
from ox_ui.tkdantic.inspector import commands_for_cls

LOGGER = logging.getLogger(__name__)

# Internal separator used in Python parameter names to encode
# nesting (e.g. ``address__city``).  The CLI uses dots instead
# (``--address.city``).
_SEP = '__'

#: The options added to every command, as (flag, Python name).  The
#: Python names cannot clash with a field's (fields never start
#: with ``ox_ui_``); the flags can, and a model with such a field
#: is refused (see :func:`_check_flag_clashes`).
PARAMS_OPTION = ('--params', 'ox_ui_params')
SHOW_PARAMS_OPTION = ('--show-params', 'ox_ui_show_params')
RPC_URL_OPTION = ('--rpc-url', 'ox_ui_rpc_url')
RPC_TIMEOUT_OPTION = ('--rpc-timeout', 'ox_ui_rpc_timeout')


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


def _make_off_flag(flag):
    """The flag that sets a boolean field to False.

    The ``no-`` goes on the last part, so a nested flag stays
    next to its group in ``--help``:

    >>> _make_off_flag('--verbose')
    '--no-verbose'
    >>> _make_off_flag('--setup.dry-run')
    '--setup.no-dry-run'
    """
    head, dot, last = flag[2:].rpartition('.')
    return f'--{head}{dot}no-{last}'


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


def _help_with_required(spec):
    """The field's help text, marked when the field is required.

    Click is never told an option is required: the values may come
    from a ``--params`` file, so pydantic checks for missing fields
    once the file and the options are merged.
    """
    help_text = spec.get('help', '')
    if _is_required(spec):
        help_text = f'{help_text}  [required]' if help_text else (
            '[required]')
    return help_text


def _scalar_option_kwargs(spec, prefix=''):
    """Build ``click.option`` arguments for a scalar spec.

    Returns ``(cli_flags, param_name, kwargs_dict)``, where
    *cli_flags* is a list: a boolean has an on and an off flag
    (``--verbose`` and ``--no-verbose``).
    """
    name = spec['name']
    flag, param = _make_flag_and_param(prefix, name)
    flags = [flag]
    gui_type = spec['gui_type']
    kwargs = {}

    if gui_type == 'bool':
        flags = [f'{flag}/{_make_off_flag(flag)}']
        kwargs['default'] = spec.get('default', False)

    elif gui_type == 'choice':
        kwargs['type'] = click.Choice(spec['choices'])
        kwargs['default'] = spec.get('default')

    elif gui_type == 'scalar_list':
        kwargs['type'] = _click_type_for(spec['inner_type'])
        kwargs['multiple'] = True

    else:
        ctype = _click_type_for(gui_type)
        kwargs['type'] = ctype
        kwargs['default'] = _cast_default(
            spec.get('default', ''), ctype,
        )

    help_text = _help_with_required(spec)
    if gui_type == 'scalar_list':
        help_text = f'{help_text}  Repeat for each value.'.strip()
    if help_text:
        kwargs['help'] = help_text

    return flags, param, kwargs


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

    Each yielded item is ``(flags, param, kwargs, meta)``:

    *   *flags* / *param* – the CLI flags (a list, see
        :func:`_scalar_option_kwargs`) and Python name.
    *   *kwargs* – ready to unpack into ``click.option``.
    *   *meta* – dict; contains ``model_class`` for
        ``model_list`` fields and ``scalar_list`` for
        ``scalar_list`` fields (empty dict otherwise).

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
            yield ([flag], param, {
                'type': str,
                'multiple': True,
                'help': _model_list_help(spec['model']),
            }, {'model_class': spec['model']})

        else:
            flags, param, kwargs = _scalar_option_kwargs(
                spec, prefix,
            )
            meta = {'scalar_list': True} if (
                gui_type == 'scalar_list') else {}
            yield (flags, param, kwargs, meta)


def _all_flags(flags):
    """Every flag in *flags*, with on/off pairs split.

    >>> _all_flags(['--name', '--verbose/--no-verbose'])
    ['--name', '--verbose', '--no-verbose']
    """
    result = []
    for flag in flags:
        result.extend(flag.split('/'))
    return result


def _check_flag_clashes(model_class, options):
    """Refuse a model whose fields' flags clash.

    A field's flag clashes when another field has it too (e.g.
    ``no_verbose`` and the off flag of ``verbose``) or when it is
    one of the added options.  Raises ``ValueError`` naming the
    flag; a field must be renamed to build a command from
    *model_class*.
    """
    field_flags = set()
    for flags, _param, _kwargs, _meta in options:
        for flag in _all_flags(flags):
            if flag in field_flags:
                raise ValueError(
                    f'Two fields of {model_class.__name__} use the '
                    f'flag {flag}.  Rename one of them.')
            field_flags.add(flag)
    added = (PARAMS_OPTION, SHOW_PARAMS_OPTION, RPC_URL_OPTION,
             RPC_TIMEOUT_OPTION)
    for flag, _param in added:
        if flag in field_flags:
            raise ValueError(
                f'{model_class.__name__} has a field with the flag '
                f'{flag}, which every generated command uses for '
                f'itself.  Rename that field.')


def _sub_model(field_info):
    """The model class of a field holding a model (or an
    optional one), else ``None``."""
    annotation, _nullable = _unwrap_optional(field_info.annotation)
    if isinstance(annotation, type) and issubclass(
            annotation, BaseModel):
        return annotation
    return None


def _models_in(field_info):
    """The model classes a field holds: its own, or its list's."""
    annotation, _nullable = _unwrap_optional(field_info.annotation)
    candidates = [annotation, *getattr(annotation, '__args__', ())]
    return [c for c in candidates if isinstance(c, type) and issubclass(
        c, BaseModel)]


def _check_no_aliases(model_class, seen=None):
    """Refuse a model (or a model in its fields) that uses aliases.

    The options, ``--params`` files and the JSON sent to a server
    are all keyed by field name; with aliases, values would be
    lost without a word.  Raises ``ValueError`` naming the field.
    """
    seen = set() if seen is None else seen
    if model_class in seen:
        return
    seen.add(model_class)
    for name, field_info in model_class.model_fields.items():
        if field_info.alias is not None or (
                field_info.validation_alias is not None):
            raise ValueError(
                f'Field {name} of {model_class.__name__} has an '
                'alias; commands cannot be built from models whose '
                'fields use aliases.')
        for sub in _models_in(field_info):
            _check_no_aliases(sub, seen)


def _add_default_parents(model_class, base, overrides):
    """*base* plus, for each nested override whose parent field is
    missing from *base*, the parent field's default, as a new dict.

    Without this, ``--setup.max-size 5`` on a field ``setup``
    whose default is ``Stage(max_size=3, repeats=7)`` would give
    a ``Stage`` with the class's default ``repeats`` instead of
    7.
    """
    result = dict(base)
    for key, value in overrides.items():
        field_info = model_class.model_fields.get(key)
        sub = _sub_model(field_info) if field_info else None
        if not isinstance(value, dict) or sub is None:
            continue
        if key not in result:
            default = resolve_default(field_info)
            if isinstance(default, BaseModel):
                result[key] = default.model_dump(mode='json')
        if isinstance(result.get(key), dict):
            result[key] = _add_default_parents(
                sub, result[key], value)
    return result


# -----------------------------------------------------------
# Kwargs reassembly (flat → nested dict)
# -----------------------------------------------------------

def _reassemble_kwargs(flat_kwargs, model_list_params,
                       scalar_list_params=()):
    """Rebuild a nested dict from flattened Click kwargs.

    *flat_kwargs* is the ``**kwargs`` dict that Click passes
    to the command callback.  Keys containing ``__`` are split
    and nested.  Keys listed in *model_list_params* have their
    tuple-of-JSON-strings parsed into lists of dicts, and keys
    in *scalar_list_params* have their tuples turned into
    lists.

    ``None`` values (unset optional options) and empty lists
    are omitted so that pydantic can apply its own defaults.
    """
    result = {}
    for key, value in flat_kwargs.items():
        # Parse JSON for model_list fields
        if key in model_list_params:
            value = [_parse_json_item(key, v) for v in value]
            if not value:
                continue
        elif key in scalar_list_params:
            value = list(value)
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


def _parse_json_item(key, text):
    """One JSON item of a ``List[BaseModel]`` option, parsed.

    Raises ``click.BadParameter`` for text that is not JSON.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        flag, _param = _make_flag_and_param(
            *_split_param(key))
        raise click.BadParameter(
            f'{text!r} is not JSON ({exc}).', param_hint=flag,
        ) from exc


def _split_param(param):
    """``(prefix, name)`` for a Python parameter name.

    >>> _split_param('setup__stops')
    ('setup', 'stops')
    >>> _split_param('stops')
    ('', 'stops')
    """
    prefix, _sep, name = param.rpartition(_SEP)
    return prefix, name


def _typed_values(ctx, flat_kwargs):
    """The options in *flat_kwargs* that the user gave.

    Options left at their defaults are dropped, so they cannot
    replace a ``--params`` file's values and pydantic applies the
    model's own defaults.  Values from ``ctx.default_map`` or the
    environment count as given.
    """
    return {
        key: value for key, value in flat_kwargs.items()
        if ctx.get_parameter_source(key) not in (
            None, ParameterSource.DEFAULT)
    }


def merge_values(base, overrides):
    """*base* with *overrides* applied, as a new dict.

    Nested dicts merge key by key; any other value in *overrides*
    (a list included) replaces the one in *base*.

    >>> merge_values({'a': 1, 'b': {'c': 2, 'd': 3}},
    ...              {'b': {'d': 4}, 'e': [5]})
    {'a': 1, 'b': {'c': 2, 'd': 4}, 'e': [5]}
    """
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(
                result.get(key), dict):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = value
    return result


def load_params_file(stream):
    """The JSON object in the open file *stream*, as a dict.

    Raises ``click.BadParameter`` when the file is not JSON or
    holds something other than an object.
    """
    name = getattr(stream, 'name', 'the file')
    try:
        data = json.load(stream)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(
            f'{name} is not JSON ({exc}).',
            param_hint=PARAMS_OPTION[0],
        ) from exc
    if not isinstance(data, dict):
        raise click.BadParameter(
            f'{name} holds a JSON {type(data).__name__}; it must '
            f'hold an object of field names and values.',
            param_hint=PARAMS_OPTION[0],
        )
    return data


# -----------------------------------------------------------
# Running the command: here, or on an XML-RPC server
# -----------------------------------------------------------

def full_rpc_url(url):
    """*url* with ``http://`` added when it has no scheme.

    >>> full_rpc_url('192.0.2.5:8765')
    'http://192.0.2.5:8765'
    >>> full_rpc_url('https://example.com/rpc')
    'https://example.com/rpc'
    """
    return url if '://' in url else f'http://{url}'


class SingleTryTransport(TimeoutTransport):
    """A :class:`TimeoutTransport` that sends each request once.

    ``xmlrpc.client.Transport`` sends a request again when the
    connection drops (meant for a stale kept-alive connection),
    even after the server got the first copy.  For a command that
    starts a job, a second copy starts a second job.
    """

    def request(self, host, handler, request_body, verbose=False):
        """Send the request once; any failure is raised."""
        return self.single_request(host, handler, request_body,
                                   verbose)


def send_to_server(url, command_name, payload, timeout=None):
    """Call *command_name* on the XML-RPC server at *url*.

    The server gets *payload*, the command's parameters as a JSON
    string, once.  This waits for its answer: up to *timeout*
    seconds, or until the command ends when *timeout* is
    ``None``.  Returns the server's result.
    """
    proxy = xmlrpc.client.ServerProxy(
        full_rpc_url(url), allow_none=True, use_builtin_types=True,
        transport=SingleTryTransport(timeout=timeout,
                                     use_builtin_types=True))
    with proxy:
        return getattr(proxy, command_name)(payload)


def _run_on_server(url, command_name, payload, timeout):
    """Send the command with :func:`send_to_server`; return its
    result.

    Every failure becomes a ``click.ClickException`` that says
    whether the command can have started on the server: only a
    refused connection means nothing was sent.  Ctrl-C stops the
    wait, not the command.
    """
    if not command_name:
        raise click.UsageError(
            'This command has no name to call on the server.')
    if not url.strip():
        raise click.UsageError(
            f'{RPC_URL_OPTION[0]} is empty; give the server\'s URL.')
    full_url = full_rpc_url(url.strip())
    click.echo(f'Sending {command_name} to {full_url}; waiting for '
               'its result.', err=True)
    try:
        return send_to_server(full_url, command_name, payload,
                              timeout)
    except xmlrpc.client.Fault as fault:
        raise click.ClickException(
            f'The server at {full_url} reported a failure in '
            f'{command_name}:\n{fault.faultString}') from fault
    except ConnectionRefusedError as exc:
        raise click.ClickException(
            f'Nothing is listening at {full_url} ({exc}); nothing '
            'was sent.') from exc
    except TimeoutError as exc:
        waited = f'within {timeout} s' if timeout else 'in time'
        raise click.ClickException(
            f'No answer from {full_url} {waited}; '
            f'{command_name} may still be running there.') from exc
    except (OSError, xmlrpc.client.Error, http.client.HTTPException,
            xml.parsers.expat.ExpatError) as exc:
        raise click.ClickException(
            f'Talking to {full_url} failed ({exc}); if the '
            f'request got through, {command_name} may be running '
            'there.') from exc
    except KeyboardInterrupt:
        click.echo(f'Stopped waiting; {command_name} may still be '
                   f'running on {full_url}.', err=True)
        raise click.Abort() from None


def _run_here(callback, instance):
    """Call *callback* with *instance*; return its result."""
    if callback is None:
        raise click.UsageError(
            'This command has no callback here; send it to a '
            f'server with {RPC_URL_OPTION[0]}.')
    return callback(instance)


# -----------------------------------------------------------
# Public API: single command
# -----------------------------------------------------------

def _added_options():
    """``click.option`` decorators for the options every command
    gets (see the module docs), as ``--help`` lists them."""
    return [
        click.option(
            *PARAMS_OPTION, type=click.File('r', encoding='utf-8'),
            default=None, metavar='FILE', help=(
                'Start from the values in this JSON file (e.g. one '
                'saved from the GUI); options given here replace the '
                "file's values.  '-' reads standard input.")),
        click.option(
            *SHOW_PARAMS_OPTION, is_flag=True, default=False, help=(
                'Print the final parameters as JSON and exit '
                'without running the command.')),
        click.option(
            *RPC_URL_OPTION, default=None, metavar='URL', help=(
                'Send the command to the XML-RPC server at this URL '
                '(e.g. http://127.0.0.1:8765, or HOST:PORT) instead '
                'of running it here, and wait for its result.')),
        click.option(
            *RPC_TIMEOUT_OPTION, type=click.FloatRange(min=0,
                                                       min_open=True),
            default=None, metavar='SECS', help=(
                f"Seconds to wait for the server's answer with "
                f'{RPC_URL_OPTION[0]} (default: until the command '
                'ends).  Giving up does not stop the command on the '
                'server.')),
    ]


def click_command_from_model(
    model_class, callback=None, name=None, help_text=None,
):
    """Build a :class:`click.Command` from a pydantic model.

    Each model field becomes a CLI option:

    *   Scalar fields (``str``, ``int``, ``float``, ``bool``,
        ``Literal[...]``) map to typed Click options.  A boolean
        gets an on and an off flag (``--verbose/--no-verbose``).
    *   Nested ``BaseModel`` fields are flattened into dotted
        options (``--address.city``).
    *   ``List[BaseModel]`` fields accept repeated JSON
        (``--stops '{...}' --stops '{...}'``), and ``List[scalar]``
        fields repeated values (``--waits 2 --waits 4``).

    The command also gets ``--params``, ``--show-params``,
    ``--rpc-url`` and ``--rpc-timeout`` (see the module docs).
    A model with a field that uses one of those flags, with two
    fields that use one flag, or with aliases raises
    ``ValueError``.

    The *callback* receives a validated model instance and its
    return value (if not ``None``) is echoed to stdout.  With
    ``--rpc-url``, the command is called on the server under
    *name* instead, and the server's result is echoed.
    """
    _check_no_aliases(model_class)
    specs = introspect_model(model_class)
    options = list(_flatten_to_options(specs))
    _check_flag_clashes(model_class, options)

    model_list_params = {
        param: meta['model_class']
        for _, param, _, meta in options
        if 'model_class' in meta
    }
    scalar_list_params = {
        param for _, param, _, meta in options
        if meta.get('scalar_list')
    }

    def invoke(**kwargs):
        """Click callback: merge, validate, run here or remotely."""
        params_file = kwargs.pop(PARAMS_OPTION[1])
        show_params = kwargs.pop(SHOW_PARAMS_OPTION[1])
        rpc_url = kwargs.pop(RPC_URL_OPTION[1])
        rpc_timeout = kwargs.pop(RPC_TIMEOUT_OPTION[1])

        typed = _typed_values(click.get_current_context(), kwargs)
        overrides = _reassemble_kwargs(
            typed, model_list_params, scalar_list_params)
        base = {} if params_file is None else load_params_file(
            params_file)
        base = _add_default_parents(model_class, base, overrides)
        payload = json.dumps(merge_values(base, overrides))
        try:
            instance = model_class.model_validate_json(payload)
        except ValidationError as exc:
            raise click.UsageError(str(exc)) from exc

        if show_params:
            click.echo(instance.model_dump_json(indent=2))
            return
        if rpc_url is not None:
            result = _run_on_server(rpc_url, name, payload,
                                    rpc_timeout)
        else:
            result = _run_here(callback, instance)
        if result is not None:
            click.echo(result)

    # Apply option decorators (reversed so --help lists them
    # as defined)
    for flags, param, kw, _meta in reversed(options):
        invoke = click.option(*flags, param, **kw)(invoke)
    for added in reversed(_added_options()):
        invoke = added(invoke)

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
            def plan_trip(self, params: TripModel):
                '''Plan a new trip.'''
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
