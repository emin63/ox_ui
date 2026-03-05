"""Core tools to build Tk GUIs from pydantic models.
"""

import json
import logging as _rawLogging
import os
from tkinter import filedialog

import tkinter as tk
from tkinter import ttk, scrolledtext
from typing import (Callable, get_origin, get_args, Literal, get_type_hints)

import click
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from ox_ui.core.simple_rpc_client import SimpleRPCCall
from ox_ui.tkdantic.callbacks import TimedCallback

LOGGER = _rawLogging.getLogger(__name__)


def _resolve_default(field_info):
    """Extract the effective default from a Pydantic FieldInfo."""
    if field_info.default is not PydanticUndefined:
        return field_info.default
    if field_info.default_factory:
        return field_info.default_factory()
    return None


def _make_choice_spec(name, annotation, default, help_text):
    """Build a GUI field spec for a ``Literal[...]`` field."""
    choices = list(get_args(annotation))
    return {"name": name, "gui_type": "choice",
            "choices": [str(c) for c in choices], "default": (
                str(default) if default is not None else str(choices[0])),
            "help": help_text}


def _make_simple_spec(name, gui_type, default, help_text):
    """Build a GUI field spec for int / float / str fields."""
    return {"name": name, "gui_type": gui_type, "default": (
        str(default) if default is not None else ""), "help": help_text}


def introspect_field(name, annotation, field_info):
    """Convert a single Pydantic field into a GUI field spec dict.

    Returns a dict with keys:
        name, gui_type, default, choices (for "choice"),
        model (for "model"/"model_list"),
        help (optional str from Field(description=...)).
    """
    default = _resolve_default(field_info)
    help_text = field_info.description or ""
    origin = get_origin(annotation)

    # Literal[...] -> option menu
    if origin is Literal:
        return _make_choice_spec(name, annotation, default, help_text)

    # bool (must check before int; bool is subclass of int)
    if annotation is bool:
        return {"name": name, "gui_type": "bool", "default": (
            default if default is not None else True), "help": help_text}

    # int / float / str
    simple_types = {int: "int", float: "float", str: "str"}
    if annotation in simple_types:
        return _make_simple_spec(name, simple_types[annotation],
                                 default, help_text)

    # Nested BaseModel -> sub-frame
    if (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
        model_help = (annotation.__doc__ or "").strip()
        return {"name": name, "gui_type": "model", "model": annotation,
                "help": model_help}

    # List[BaseModel] -> repeatable sub-frame
    if origin is list:
        args = get_args(annotation)
        if (args and isinstance(args[0], type) and issubclass(
                args[0], BaseModel)):
            model_help = (args[0].__doc__ or "").strip()
            return {"name": name, "gui_type": "model_list",
                    "model": args[0], "help": model_help}

    # Fallback: treat as string
    return _make_simple_spec(name, "str", default, help_text)


def introspect_model(model_class):
    """Return a list of GUI field spec dicts for *model_class*."""
    specs = []
    hints = get_type_hints(model_class)
    for field_name, field_info in model_class.model_fields.items():
        annotation = hints[field_name]
        specs.append(introspect_field(field_name, annotation, field_info))
    return specs


def _all_fields_have_defaults(model_class) -> bool:
    """Return True if every field has a default value."""
    for field_info in model_class.model_fields.values():
        if field_info.default is PydanticUndefined and (
                field_info.default_factory is None):
            return False
    return True


# -------------------------------------------------------------------
# Recursive GUI builder and value collector
# -------------------------------------------------------------------
#
# build_fields_in_frame returns a "widget tree" — a dict mapping
# each field name to a tagged tuple so collect_values knows how to
# recurse:
#
#   scalar fields : (spec, tk_variable)
#   model  fields : ("__model__", sub_widget_tree)
#   model_list    : ("__model_list__", _LegSection)
#
# The sentinel strings "__model__" and "__model_list__" in
# position 0 distinguish nested entries from scalar (spec, var)
# tuples, since a spec dict will never equal those strings.
# -------------------------------------------------------------------

_MODEL_TAG = "__model__"
_MODEL_LIST_TAG = "__model_list__"


# -------------------------------------------------------------------
# Tooltip helper
# -------------------------------------------------------------------

def add_tooltip(widget, text, delay_ms=400, wrap_px=300):
    """Attach a hover tooltip to any tkinter widget.

    Usage::

        label = ttk.Label(parent, text="price")
        add_tooltip(label, "Limit price for the order.")
    """
    state = {"tip_window": None, "after_id": None}

    def _schedule(_event=None):
        _cancel()
        state["after_id"] = widget.after(delay_ms, _show)

    def _cancel(_event=None):
        if state["after_id"]:
            widget.after_cancel(state["after_id"])
            state["after_id"] = None
        _hide()

    def _show():
        if state["tip_window"] or not text:
            return
        x = widget.winfo_rootx() + 20
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        tw = tk.Toplevel(widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=text, justify="left", background="#ffffe0",
                         relief="solid", borderwidth=1, wraplength=wrap_px,
                         font=("TkDefaultFont", 9))
        label.pack()
        state["tip_window"] = tw

    def _hide():
        if state["tip_window"]:
            state["tip_window"].destroy()
            state["tip_window"] = None

    widget.bind("<Enter>", _schedule, add="+")
    widget.bind("<Leave>", _cancel, add="+")
    widget.bind("<ButtonPress>", _cancel, add="+")


class CollapsibleFrame:
    """A frame with a clickable header that collapses/expands.

    Uses composition rather than inheritance to wrap a
    ``ttk.Frame``.  Build child widgets inside ``self.inner``,
    not directly in the frame.
    """

    def __init__(self, parent, text="", collapsed=False, padding=6,
                 tooltip="", **kwargs):
        self._frame = ttk.Frame(parent, **kwargs)
        self._expanded = not collapsed
        self._base_text = text

        # Toggle button acts as the section header
        self._toggle_btn = ttk.Button(
            self._frame, text=self._label_text(),
            command=self.toggle, style="Toolbutton")
        self._toggle_btn.pack(fill="x", anchor="w")
        if tooltip:
            add_tooltip(self._toggle_btn, tooltip)

        # Inner frame holds the actual child widgets
        self.inner = ttk.Frame(self._frame, padding=padding)
        if self._expanded:
            self.inner.pack(fill="both", expand=True)

    # -- geometry delegation ------------------------------------

    def grid(self, **kwargs):
        """Delegate grid placement to the wrapped frame."""
        self._frame.grid(**kwargs)

    def pack(self, **kwargs):
        """Delegate pack placement to the wrapped frame."""
        self._frame.pack(**kwargs)

    def destroy(self):
        """Destroy the wrapped frame and all its children."""
        self._frame.destroy()

    def toggle(self):
        """Toggle the collapsed/expanded state."""
        self._expanded = not self._expanded
        self._toggle_btn.configure(text=self._label_text())
        if self._expanded:
            self.inner.pack(fill="both", expand=True)
        else:
            self.inner.pack_forget()

    def set_text(self, text):
        """Update the base label text."""
        self._base_text = text
        self._toggle_btn.configure(text=self._label_text())

    def _label_text(self):
        arrow = "\u25BC" if self._expanded else "\u25B6"
        return f"{arrow}  {self._base_text}"


# -------------------------------------------------------------------
# Scalar widget layout helpers
# -------------------------------------------------------------------

def _place_scalar_widget(parent, spec, *, row, label_col, widget_col,
                         compact):
    """Create label + widget for a single scalar field.

    *compact* selects smaller widget sizes for horizontal layout.
    Returns the tk variable associated with the widget.
    """
    name = spec["name"]
    gui_type = spec["gui_type"]
    help_text = spec.get("help", "")

    label = ttk.Label(parent, text=name)
    label.grid(row=row, column=label_col, sticky="w", padx=(0, 4), pady=2)
    if help_text:
        add_tooltip(label, help_text)

    if gui_type == "choice":
        var = tk.StringVar(value=spec["default"])
        menu = ttk.OptionMenu(
            parent, var, spec["default"], *spec["choices"])
        menu.grid(row=row, column=widget_col,
                  sticky="ew", pady=2, padx=(0, 8))

    elif gui_type == "bool":
        var = tk.BooleanVar(value=spec["default"])
        chk = ttk.Checkbutton(parent, variable=var)
        chk.grid(row=row, column=widget_col,
                 sticky="w", pady=2, padx=(0, 8))

    else:  # str, int, float
        var = tk.StringVar(value=spec.get("default", ""))
        width = 15 if compact else 32
        entry = ttk.Entry(parent, textvariable=var, width=width)
        entry.grid(row=row, column=widget_col,
                   sticky="ew", pady=2, padx=(0, 8))

    return var


def _lay_out_scalars(
    parent: ttk.Frame,
    scalar_specs: list[dict],
    num_cols: int,
    compact: bool,
) -> tuple[dict, int]:
    """Place scalar widgets in a multi-column grid.

    Returns (widgets_dict, next_row) where *widgets_dict* maps
    field names to (spec, tk_variable) pairs and *next_row* is
    the first grid row available after the placed widgets.
    """
    widgets: dict = {}

    for i, spec in enumerate(scalar_specs):
        r = i // num_cols
        c = (i % num_cols) * 2
        var = _place_scalar_widget(
            parent, spec,
            row=r, label_col=c,
            widget_col=c + 1,
            compact=compact,
        )
        widgets[spec["name"]] = (spec, var)

    for c in range(num_cols):
        parent.columnconfigure(c * 2 + 1, weight=1)

    next_row = -(-len(scalar_specs) // num_cols)  # ceil div
    return widgets, next_row


# -------------------------------------------------------------------
# Nested model layout helpers
# -------------------------------------------------------------------

def _place_nested_model(parent: ttk.Frame, spec: dict, *, row: int,
                        columnspan: int, max_horizontal: int) -> tuple:
    """Create a :class:`CollapsibleFrame` for a sub-model.

    Returns a ``(_MODEL_TAG, sub_widgets)`` tuple.
    """
    sub_model = spec["model"]
    sub_specs = introspect_model(sub_model)
    model_doc = (
        spec.get("help", "")
        or (sub_model.__doc__ or "").strip()
    )
    child_frame = CollapsibleFrame(
        parent, text=sub_model.__name__,
        collapsed=_all_fields_have_defaults(sub_model),
        padding=6, tooltip=model_doc)
    child_frame.grid(row=row, column=0, columnspan=columnspan,
                     sticky="ew", pady=4)
    sub_widgets = build_fields_in_frame(
        child_frame.inner, sub_specs, max_horizontal=max_horizontal)
    return _MODEL_TAG, sub_widgets


def _place_model_list(
    parent: ttk.Frame,
    spec: dict,
    *,
    row: int,
    columnspan: int,
    max_horizontal: int,
) -> tuple:
    """Create a ``_LegSection`` for a ``List[SubModel]`` field.

    Returns a ``(_MODEL_LIST_TAG, section)`` tuple.
    """
    container = ttk.Frame(parent)
    container.grid(
        row=row, column=0,
        columnspan=columnspan,
        sticky="ew", pady=4,
    )
    section = _LegSection(
        container, spec["model"],
        max_horizontal=max_horizontal,
    )
    return _MODEL_LIST_TAG, section


_NESTED_PLACERS: dict[str, Callable] = {
    "model": _place_nested_model,
    "model_list": _place_model_list,
}


# -------------------------------------------------------------------
# Recursive field builder
# -------------------------------------------------------------------

def build_fields_in_frame(
    parent_frame: ttk.Frame,
    field_specs: list[dict],
    max_horizontal: int = 4,
) -> dict:
    """Populate *parent_frame* with widgets for *field_specs*.

    Sub-model fields get their own ``LabelFrame``;
    ``List[SubModel]`` fields support dynamic add/remove of
    repeated sub-frames (legs).  All nesting is handled
    recursively by build_fields_in_frame.

    Layout tiers for scalar (non-model) fields, based on count
    *n* versus *max_horizontal* (mh):

    * n <= mh      -- single horizontal row, compact widgets
    * n <= 2 * mh  -- 3-column grid, compact widgets
    * n >  2 * mh  -- single-column vertical list, full-width
    """
    nested_types = set(_NESTED_PLACERS)

    scalar_specs = [
        s for s in field_specs
        if s["gui_type"] not in nested_types
    ]
    nested_specs = [
        s for s in field_specs
        if s["gui_type"] in nested_types
    ]

    # --- Choose scalar grid geometry ---
    num_scalars = len(scalar_specs)
    if num_scalars <= max_horizontal:
        num_cols = max(num_scalars, 1)
        compact = True
    elif num_scalars <= 2 * max_horizontal:
        num_cols = 3
        compact = True
    else:
        num_cols = 1
        compact = False

    # --- Lay out scalar fields ---
    widgets, next_row = _lay_out_scalars(
        parent_frame, scalar_specs, num_cols, compact,
    )

    # --- Lay out nested model / model-list fields ---
    for spec in nested_specs:
        placer = _NESTED_PLACERS[spec["gui_type"]]
        tag, value = placer(parent_frame, spec,
                            row=next_row, columnspan=num_cols * 2,
                            max_horizontal=max_horizontal)
        widgets[spec["name"]] = (tag, value)
        next_row += 1

    return widgets


# -------------------------------------------------------------------
# Value collection and loading
# -------------------------------------------------------------------

def collect_values(widgets, path=()):
    """Recursively walk a widget tree and return a nested dict."""
    result = {}
    for name, entry in widgets.items():
        try:
            # Nested model
            if entry[0] == _MODEL_TAG:
                result[name] = collect_values(
                    entry[1], path=(name,) + path,
                )
                continue

            # List of models
            if entry[0] == _MODEL_LIST_TAG:
                section = entry[1]
                result[name] = section.collect_all()
                continue

            # Scalar field: entry is (spec, var)
            spec, var = entry
            raw = var.get()
            gui_type = spec["gui_type"]
            if gui_type == "int":
                result[name] = int(raw)
            elif gui_type == "float":
                result[name] = float(raw)
            elif gui_type == "bool":
                result[name] = bool(var.get())
            else:
                result[name] = str(raw)
        except ValueError as problem:
            LOGGER.exception(
                'Problem in collect_values for path=%s',
                path,
            )
            joined = "/".join((name,) + path)
            msg = f"For {joined}: {problem}"
            raise ValueError(msg) from problem
        except Exception:
            LOGGER.exception(
                'Problem in collect_values for path=%s',
                path,
            )
            raise

    return result


def load_values(widgets, data):
    """Recursively set widget values from a plain dict.

    This is the inverse of :func:`collect_values`.
    """
    if not isinstance(data, dict):
        return

    for name, entry in widgets.items():
        if name not in data:
            continue
        value = data[name]

        # Nested model
        if entry[0] == _MODEL_TAG:
            load_values(entry[1], value)
            continue

        # List of models
        if entry[0] == _MODEL_LIST_TAG:
            section = entry[1]
            if isinstance(value, list):
                section.load_all(value)
            continue

        # Scalar field: entry is (spec, var)
        _spec, var = entry
        var.set(value)


# -------------------------------------------------------------------
# Dynamic list-of-models section (e.g. order legs)
# -------------------------------------------------------------------

class _LegSection:
    """Manages a dynamic list of sub-model frames.

    Uses build_fields_in_frame recursively so legs can themselves
    contain nested sub-models.  Starts with one instance.
    """

    def __init__(self, parent, model_class, max_horizontal=4):
        self.model_class = model_class
        self._max_horizontal = max_horizontal
        self._legs = []  # list of (frame, widget_tree)

        model_doc = (model_class.__doc__ or "").strip()
        lbl = ttk.Label(
            parent,
            text=f"  {model_class.__name__} List  ",
            font=("TkDefaultFont", 9, "bold"),
        )
        if model_doc:
            add_tooltip(lbl, model_doc)
        self._outer = ttk.LabelFrame(
            parent,
            labelwidget=lbl,
            padding=6,
        )
        self._outer.pack(fill="x")

        btn_row = ttk.Frame(self._outer)
        btn_row.pack(fill="x", pady=(0, 4))
        ttk.Button(
            btn_row, text="+ Add",
            command=self._add_leg,
        ).pack(side="left")
        ttk.Button(
            btn_row, text="\u2212 Remove Last",
            command=self._remove_leg,
        ).pack(side="left", padx=(6, 0))
        self._count_label = ttk.Label(btn_row, text="")
        self._count_label.pack(side="left", padx=8)

        self._container = ttk.Frame(self._outer)
        self._container.pack(fill="x")

        # Start with one entry
        self._add_leg()

    def _update_count(self):
        n = len(self._legs)
        self._count_label.config(text=f"({n} item(s))")

    def _add_leg(self):
        idx = len(self._legs) + 1
        model_doc = (
            self.model_class.__doc__ or ""
        ).strip()
        frame = CollapsibleFrame(
            self._container,
            text=f"{self.model_class.__name__} #{idx}",
            collapsed=_all_fields_have_defaults(
                self.model_class,
            ),
            padding=4,
            tooltip=model_doc,
        )
        frame.pack(fill="x", pady=2)
        specs = introspect_model(self.model_class)
        widget_tree = build_fields_in_frame(
            frame.inner, specs,
            max_horizontal=self._max_horizontal,
        )
        self._legs.append((frame, widget_tree))
        self._update_count()

    def _remove_leg(self):
        if len(self._legs) <= 1:
            return  # keep at least one
        frame, _ = self._legs.pop()
        frame.destroy()
        self._update_count()

    def collect_all(self):
        """Return a list of plain dicts, one per entry."""
        return [collect_values(wt) for _, wt in self._legs]

    def load_all(self, items: list[dict]):
        """Clear legs and recreate from *items*."""
        while self._legs:
            frame, _ = self._legs.pop()
            frame.destroy()
        self._update_count()

        for item in items:
            self._add_leg()
            _, widget_tree = self._legs[-1]
            load_values(widget_tree, item)


# -------------------------------------------------------------------
# Scrollable canvas helper
# -------------------------------------------------------------------

def setup_scrollable_canvas(parent):
    """Add a vertically-scrollable canvas to *parent*.

    Returns ``(canvas, inner_frame)`` where *inner_frame* is the
    frame that should receive all child widgets.  Mouse-wheel
    bindings are configured for both Windows and Linux.
    """
    canvas = tk.Canvas(
        parent, borderwidth=0, highlightthickness=0,
    )
    vscroll = ttk.Scrollbar(
        parent, orient="vertical", command=canvas.yview,
    )
    inner = ttk.Frame(canvas)
    inner.bind(
        "<Configure>",
        lambda e: canvas.configure(
            scrollregion=canvas.bbox("all"),
        ),
    )
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=vscroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    vscroll.pack(side="right", fill="y")

    # Windows / macOS mouse-wheel
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind("<MouseWheel>", _on_mousewheel)
    canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
    canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))

    return canvas, inner


class ModelCommandWindow(tk.Toplevel):
    """A pop-up window whose input fields are introspected from
    a Pydantic BaseModel.

    Sub-models each get their own labelled frame;
    ``List[SubModel]`` fields support dynamic add/remove of
    repeated sub-frames (legs).  All nesting is handled
    recursively by :func:`build_fields_in_frame`.
    """

    def __init__(self, master, *, command, url_var: tk.StringVar,
                 timeout_var: tk.IntVar, max_horizontal: int = 4):
        super().__init__(master)
        self.command = command
        self.model_class = self.command.parameters[0]
        self._server_config = {
            "url_var": url_var,
            "timeout_var": timeout_var,
        }
        self.title(command.title)
        self.minsize(720, 800)

        _canvas, inner = setup_scrollable_canvas(self)
        self._build_fields(inner, max_horizontal)
        self._build_buttons(inner)
        self._build_results(inner)

    def _build_fields(self, inner, max_horizontal):
        """Create the model fields section."""
        model_doc = (self.model_class.__doc__ or "").strip()
        lbl = ttk.Label(inner, text=f"  {self.model_class.__name__}  ",
                        font=("TkDefaultFont", 9, "bold"))
        if model_doc:
            add_tooltip(lbl, model_doc)
        fields_frame = ttk.LabelFrame(
            inner, labelwidget=lbl, padding=6)
        fields_frame.pack(fill="x", padx=8, pady=(8, 4))

        top_specs = introspect_model(self.model_class)
        self._widget_tree = build_fields_in_frame(
            fields_frame, top_specs, max_horizontal=max_horizontal)

    def _build_buttons(self, inner):
        """Create Submit / Dump / Load buttons."""
        btn_frame = ttk.Frame(inner, padding=4)
        btn_frame.pack(fill="x", padx=8, pady=(4, 0))

        self._submit_btn = ttk.Button(btn_frame, text="Submit",
                                      command=self._on_submit)
        self._submit_btn.pack(side="left")

        ttk.Button(btn_frame, text="Dump", command=self._on_dump,
                   ).pack(side="left", padx=(4, 0))

        ttk.Button(btn_frame, text="Load", command=self._on_load,
                   ).pack(side="left", padx=(4, 0))

        self._status_label = ttk.Label(btn_frame, text="")
        self._status_label.pack(side="left", padx=8)

    def _build_results(self, inner):
        """Create the results display area."""
        result_frame = ttk.LabelFrame(
            inner, text="Result", padding=6,
        )
        result_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self._result_text = scrolledtext.ScrolledText(
            result_frame, height=8, wrap="word", state="disabled",
            font=("Consolas", 10))
        self._result_text.pack(fill="both", expand=True)

    # -- Dump / Load --------------------------------------------

    def _on_dump(self):
        """Validate and save the form as a JSON file."""
        try:
            raw_payload = collect_values(self._widget_tree)
        except (ValueError, TypeError) as exc:
            self._show_result(f"INPUT ERROR: {exc}")
            return

        try:
            order = self.model_class.model_validate(raw_payload)
        except ValidationError as exc:
            self._show_result(f"VALIDATION ERROR:\n{exc}")
            return

        path = filedialog.asksaveasfilename(
            parent=self, title="Save order as JSON", defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*"),])
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(order.model_dump_json(indent=2))
            self._show_result(f"Saved to {path}")
        except OSError as exc:
            self._show_result(f"FILE ERROR: {exc}")

    def _on_load(self):
        """Load a previously-dumped JSON file into the form."""
        path = filedialog.askopenfilename(
            parent=self,
            title="Load order from JSON",
            filetypes=[
                ("JSON files", "*.json"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            self._show_result(f"LOAD ERROR: {exc}")
            return

        # Validate so the user sees Pydantic errors early
        try:
            self.model_class.model_validate(data)
        except ValidationError as exc:
            self._show_result(f"VALIDATION WARNING:\n{exc}")
            # Continue anyway – fill what we can

        try:
            load_values(self._widget_tree, data)
            self._show_result(f"Loaded from {path}")
        except (ValueError, TypeError, KeyError) as exc:
            self._show_result(f"LOAD ERROR: {exc}")

    # -- Submit -------------------------------------------------

    def _on_submit(self):
        """Validate with Pydantic, then send via XML-RPC."""
        try:
            raw_payload = collect_values(self._widget_tree)
        except (ValueError, TypeError) as exc:
            self._show_result(f"INPUT ERROR: {exc}")
            return

        try:
            order = self.model_class.model_validate(raw_payload)
        except ValidationError as exc:
            self._show_result(f"VALIDATION ERROR:\n{exc}")
            return

        if self.command.callback is not None:
            result = self.command.callback(order)
            self._on_call_done(result)
            return

        url = self._server_config["url_var"].get().strip()
        if not url:
            self._show_result("ERROR: Server URL is empty.")
            return

        self._submit_btn.config(state="disabled")
        self._status_label.config(text="Calling\u2026")

        timeout = self._server_config["timeout_var"].get()
        call_thread = SimpleRPCCall(
            url=url, command_name=self.command.name, cmd_args=order,
            rpc_timeout=timeout)
        call_thread.after = TimedCallback(
            interval=0, function=self._on_call_done, args=[call_thread])
        call_thread.start()

    def _on_call_done(self, cmd_result):
        """Handle the XML-RPC call completion."""
        if not isinstance(cmd_result, str):
            cmd_result = str(
                getattr(cmd_result, 'result', cmd_result),
            )
        self._submit_btn.config(state="normal")
        self._status_label.config(text="")
        self._show_result(cmd_result)

    def _show_result(self, text: str):
        """Replace the result pane contents with *text*."""
        self._result_text.config(state="normal")
        self._result_text.delete("1.0", "end")
        self._result_text.insert("end", text)
        self._result_text.config(state="disabled")


class MainApp(tk.Tk):
    """Root window with server URL and command buttons."""

    def __init__(self, title, model_commands, bind=None, port=None):
        super().__init__()
        self.title(title)
        self.minsize(400, 300)
        self._model_commands = model_commands
        self._build_url_frame(bind, port)
        self._build_command_buttons()

    @staticmethod
    def get_default_server_url(bind=None, port=None) -> str:
        """Return the default XML-RPC server URL.

        *IMPORTANT*: Use IP address since hostname lookups will be *SLOW*.
        """
        bind = bind or '127.0.0.1'
        port = port or 8765
        return f"http://{bind}:{port}"

    def _build_url_frame(self, bind, port):
        """Create the server URL and timeout controls."""
        url_frame = ttk.LabelFrame(self, text="XML-RPC Server", padding=6)
        url_frame.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(url_frame, text="URL:").pack(side="left")
        self._url_var = tk.StringVar(value=self.get_default_server_url(
            bind, port))
        url_entry = ttk.Entry(url_frame, textvariable=self._url_var, width=50)
        url_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))

        ttk.Label(url_frame, text="Timeout:").pack(side="left", padx=(6, 0))

        self._timeout_var = tk.IntVar(value=20)
        timeout_entry = ttk.Entry(url_frame, textvariable=self._timeout_var,
                                  width=5)
        timeout_entry.pack(side="left", padx=(6, 0))

    def _build_command_buttons(self):
        """Create a button for each available command."""
        btn_frame = ttk.LabelFrame(self, text="Commands", padding=6)
        btn_frame.pack(fill="both", expand=True,
                       padx=10, pady=(4, 10))

        for cmd_key, info in self._model_commands.items():
            LOGGER.debug('Preparing command for %s', cmd_key)
            btn = ttk.Button(btn_frame, text=info.title,
                             command=lambda i=info: self._open_command(i))
            btn.pack(fill="x", pady=2)
            if info.parameters:
                model_doc = (info.parameters[0].__doc__ or "").strip()
                if model_doc:
                    add_tooltip(btn, model_doc)

    def _open_command(self, info):
        """Open a ModelCommandWindow for *info*."""
        assert len(info.parameters) <= 1
        ModelCommandWindow(
            self, command=info, url_var=self._url_var,
            timeout_var=self._timeout_var, max_horizontal=4)

