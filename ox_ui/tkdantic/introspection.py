"""Introspect pydantic models into field-spec dicts.

This module contains the pure-logic introspection layer shared
by both the Tk GUI builder (:mod:`builder`) and the Click CLI
builder (:mod:`cli_builder`).  It has **no** tkinter or Click
dependency so it can be imported anywhere.

The central data structure is a *field spec* — a plain dict
with at least these keys::

    {
        "name":     str,   # field name
        "gui_type": str,   # "str" | "int" | "float" | "bool"
                           #   | "choice" | "filepath"
                           #   | "model" | "model_list"
        "default":  ...,   # resolved default value (may be "")
        "help":     str,   # description from Field(description=...)
    }

Additional keys appear for specific types:

*   ``choices`` — list of strings (for ``"choice"``).
*   ``model``  — the ``BaseModel`` subclass
    (for ``"model"`` and ``"model_list"``).
*   ``nullable`` — ``True`` when the field accepts ``None``.
*   ``mode``   — ``"save"`` or ``"open"`` (for ``"filepath"``).
*   ``filetypes`` — list of (label, pattern) tuples
    (for ``"filepath"``).
*   ``defaultextension`` — e.g. ``".csv"`` (for ``"filepath"``).
"""

import pathlib
import types
from typing import (
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel
from pydantic_core import PydanticUndefined


# -----------------------------------------------------------
# Default resolution
# -----------------------------------------------------------

def resolve_default(field_info):
    """Extract the effective default from a FieldInfo.

    Returns the static default, the result of calling
    ``default_factory``, or ``None`` if neither is set.
    """
    if field_info.default is not PydanticUndefined:
        return field_info.default
    if field_info.default_factory:
        return field_info.default_factory()
    return None


# -----------------------------------------------------------
# Spec builders for individual field flavours
# -----------------------------------------------------------

def _make_choice_spec(name, annotation, default, help_text):
    """Build a field spec for a ``Literal[...]`` field."""
    choices = list(get_args(annotation))
    return {
        "name": name,
        "gui_type": "choice",
        "choices": [str(c) for c in choices],
        "default": (
            str(default)
            if default is not None
            else str(choices[0])
        ),
        "help": help_text,
    }


def _make_simple_spec(
    name, gui_type, default, help_text, nullable=False,
):
    """Build a field spec for int / float / str fields."""
    return {
        "name": name,
        "gui_type": gui_type,
        "default": (
            str(default) if default is not None else ""
        ),
        "help": help_text,
        "nullable": nullable,
    }


def _make_filepath_spec(name, default, help_text, field_info,
                        nullable=False):
    """Build a field spec for a ``pathlib.Path`` field.

    Dialog hints (``mode``, ``filetypes``, ``defaultextension``)
    are pulled from ``field_info.json_schema_extra`` when present.
    The conservative default is ``mode="save"`` so the user can
    type a path that does not yet exist.
    """
    extras = {}
    if isinstance(field_info.json_schema_extra, dict):
        extras = field_info.json_schema_extra

    return {
        "name": name,
        "gui_type": "filepath",
        "default": (
            str(default) if default is not None else ""
        ),
        "help": help_text,
        "nullable": nullable,
        "mode": extras.get("mode", "save"),
        "filetypes": extras.get(
            "filetypes",
            [("All files", "*.*")],
        ),
        "defaultextension": extras.get("defaultextension", ""),
    }


# -----------------------------------------------------------
# Single-field introspection
# -----------------------------------------------------------

def _unwrap_optional(annotation):
    """Unwrap ``Optional[X]`` (Union[X, None]) if present.

    Returns ``(inner_annotation, is_nullable)``.
    """
    origin = get_origin(annotation)
    is_union = origin is Union or (
        hasattr(types, 'UnionType')
        and isinstance(annotation, types.UnionType)
    )
    if not is_union:
        return annotation, False

    args = [
        a for a in get_args(annotation)
        if a is not type(None)
    ]
    if len(args) == 1:
        return args[0], True
    return annotation, False


def introspect_field(name, annotation, field_info):
    """Convert a single pydantic field into a field spec dict.

    Returns a dict with keys documented in the module
    docstring.  Handles ``Optional``, ``Literal``, ``bool``,
    ``int``, ``float``, ``str``, ``pathlib.Path``,
    nested ``BaseModel``, and ``List[BaseModel]``.
    """
    default = resolve_default(field_info)
    help_text = field_info.description or ""

    # --- Unwrap Optional[X] ---
    annotation, nullable = _unwrap_optional(annotation)
    origin = get_origin(annotation)

    # Also nullable when default is explicitly None
    if (
        default is None
        and field_info.default is not PydanticUndefined
    ):
        nullable = True

    # Literal[...] → choice
    if origin is Literal:
        return _make_choice_spec(
            name, annotation, default, help_text,
        )

    # bool (must check before int; bool is subclass of int)
    if annotation is bool:
        return {
            "name": name,
            "gui_type": "bool",
            "default": (
                default if default is not None else True
            ),
            "help": help_text,
        }

    # int / float / str
    simple_types = {int: "int", float: "float", str: "str"}
    if annotation in simple_types:
        return _make_simple_spec(
            name, simple_types[annotation],
            default, help_text, nullable=nullable,
        )

    # pathlib.Path / PurePath → file browser
    if isinstance(annotation, type) and issubclass(
        annotation, pathlib.PurePath,
    ):
        return _make_filepath_spec(
            name, default, help_text, field_info,
            nullable=nullable,
        )

    # Nested BaseModel → sub-model
    if isinstance(annotation, type) and issubclass(
        annotation, BaseModel,
    ):
        model_help = (annotation.__doc__ or "").strip()
        return {
            "name": name,
            "gui_type": "model",
            "model": annotation,
            "help": model_help,
        }

    # List[BaseModel] → repeatable sub-model
    if origin is list:
        args = get_args(annotation)
        if (
            args
            and isinstance(args[0], type)
            and issubclass(args[0], BaseModel)
        ):
            model_help = (args[0].__doc__ or "").strip()
            return {
                "name": name,
                "gui_type": "model_list",
                "model": args[0],
                "help": model_help,
            }

    # Fallback: treat as string
    return _make_simple_spec(
        name, "str", default, help_text, nullable=nullable,
    )


# -----------------------------------------------------------
# Whole-model introspection
# -----------------------------------------------------------

def introspect_model(model_class):
    """Return a list of field spec dicts for *model_class*.

    Iterates over every field in the pydantic model and
    produces a spec dict via :func:`introspect_field`.
    """
    specs = []
    hints = get_type_hints(model_class)
    for field_name, field_info in model_class.model_fields.items():
        annotation = hints[field_name]
        specs.append(
            introspect_field(field_name, annotation, field_info)
        )
    return specs


def all_fields_have_defaults(model_class) -> bool:
    """Return True if every field has a default value."""
    for field_info in model_class.model_fields.values():
        if (
            field_info.default is PydanticUndefined
            and field_info.default_factory is None
        ):
            return False
    return True
