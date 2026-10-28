"""Tests for the Tk form builder in :mod:`ox_ui.tkdantic.builder`.

All tests here need a display; they use the ``tk_root`` fixture
from ``conftest.py`` and are skipped when Tk cannot start.  File
dialogs are replaced with :class:`ConstantFn` so nothing blocks.
"""

import json
import logging
import pathlib
import tkinter as tk
from tkinter import filedialog, ttk
from typing import List, Literal, Optional

from pydantic import BaseModel, Field
import pytest

from ox_ui.tkdantic import builder
from ox_ui.tkdantic.builder import (
    CollapsibleFrame,
    MainApp,
    ModelCommandWindow,
    _ModelListSection,
    _ScalarListWidget,
    _dump_sub_model,
    _load_sub_model,
    add_tooltip,
    build_fields_in_frame,
    collect_values,
    load_values,
    setup_scrollable_canvas,
)
from ox_ui.tkdantic.command import Command
from ox_ui.tkdantic.introspection import introspect_model

from .tk_helpers import ConstantFn, find_widgets, is_packed, text_content


# ---------------------------------------------------------------
# Test models
# ---------------------------------------------------------------

class Address(BaseModel):
    """Mailing address."""

    street: str
    city: str = 'Springfield'


class Stop(BaseModel):
    """One stop on a trip."""

    miles: float
    month: str = 'Dec'


class Trip(BaseModel):
    """A trip with one field of every kind."""

    traveler: str
    seats: int
    speed: float = 9.5
    urgent: bool = False
    transport: Literal['train', 'bus'] = 'train'
    note: Optional[str] = None
    out_file: pathlib.Path = Field(
        default=pathlib.Path('out.csv'),
        json_schema_extra={'mode': 'open'},
    )
    address: Address
    stops: List[Stop]
    tags: List[str] = Field(default_factory=lambda: ['x'])
    sizes: List[int] = Field(default_factory=list)


TRIP_DATA = {
    'traveler': 'Ann',
    'seats': 4,
    'speed': 2.25,
    'urgent': True,
    'transport': 'bus',
    'note': 'rush',
    'out_file': 'trips.csv',
    'address': {'street': '1 Main', 'city': 'Shelbyville'},
    'stops': [
        {'miles': 100.0, 'month': 'Jan'},
        {'miles': 110.5, 'month': 'Feb'},
    ],
    'tags': ['a', 'b'],
    'sizes': [1, 2, 3],
}


class Inner(BaseModel):
    """Innermost model."""

    value: int = 0


class Middle(BaseModel):
    """Middle model."""

    inner: Inner = Field(default_factory=Inner)


class Deep(BaseModel):
    """Three levels of nesting."""

    middle: Middle = Field(default_factory=Middle)


class Point(BaseModel):
    """Two coordinates, both with defaults."""

    x: int = 0
    y: int = 0


class Small(BaseModel):
    """A model with a single required field."""

    name: str


def scalar_model_specs(count):
    """Return *count* str field specs named f0, f1, ..."""
    return [
        {'name': f'f{i}', 'gui_type': 'str', 'default': '',
         'help': ''}
        for i in range(count)
    ]


def build_trip_tree(tk_root):
    """Build the Trip form in a frame and return its tree."""
    frame = ttk.Frame(tk_root)
    frame.pack()
    return build_fields_in_frame(frame, introspect_model(Trip))


def write_json(path, data):
    """Write *data* as JSON to *path*."""
    path.write_text(json.dumps(data), encoding='utf-8')


def read_json(path):
    """Return the JSON content of *path*."""
    return json.loads(path.read_text(encoding='utf-8'))


def patch_dialog(monkeypatch, name, value):
    """Replace ``filedialog.<name>`` with a ConstantFn."""
    fake = ConstantFn(value)
    monkeypatch.setattr(filedialog, name, fake)
    return fake


# ---------------------------------------------------------------
# Tooltip and CollapsibleFrame
# ---------------------------------------------------------------

class TestTooltip:
    """Tests for :func:`add_tooltip`."""

    def test_enter_shows_and_leave_hides(self, tk_root):
        """Entering shows a tooltip window; leaving destroys it."""
        label = ttk.Label(tk_root, text='speed')
        label.pack()
        add_tooltip(label, 'Average speed.', delay_ms=0)
        label.event_generate('<Enter>')
        tk_root.update()
        tips = find_widgets(label, tk.Toplevel)
        assert len(tips) == 1
        assert find_widgets(tips[0], tk.Label, 'Average speed.')
        label.event_generate('<Leave>')
        tk_root.update()
        assert not find_widgets(label, tk.Toplevel)

    def test_empty_text_shows_nothing(self, tk_root):
        """A tooltip with empty text never opens a window."""
        label = ttk.Label(tk_root, text='x')
        label.pack()
        add_tooltip(label, '', delay_ms=0)
        label.event_generate('<Enter>')
        tk_root.update()
        assert not find_widgets(label, tk.Toplevel)

    def test_leave_before_delay_cancels(self, tk_root):
        """Leaving before the delay cancels the pending tooltip."""
        label = ttk.Label(tk_root, text='x')
        label.pack()
        add_tooltip(label, 'tip', delay_ms=60000)
        label.event_generate('<Enter>')
        label.event_generate('<ButtonPress>', button=1)
        tk_root.update()
        assert not find_widgets(label, tk.Toplevel)


class TestCollapsibleFrame:
    """Tests for :class:`CollapsibleFrame`."""

    def test_toggle(self, tk_root):
        """toggle() hides and shows the inner frame and arrow."""
        frame = CollapsibleFrame(tk_root, text='Box')
        frame.pack()
        button = find_widgets(frame._frame, ttk.Button)[0]
        assert is_packed(frame.inner)
        assert str(button.cget('text')) == '▼  Box'
        frame.toggle()
        assert not is_packed(frame.inner)
        assert str(button.cget('text')) == '▶  Box'
        button.invoke()
        assert is_packed(frame.inner)

    def test_collapsed_and_set_text(self, tk_root):
        """A collapsed frame starts hidden; set_text relabels it."""
        frame = CollapsibleFrame(tk_root, text='A', collapsed=True,
                                 tooltip='help')
        frame.grid(row=0, column=0)
        assert not is_packed(frame.inner)
        frame.set_text('B')
        button = find_widgets(frame._frame, ttk.Button)[0]
        assert str(button.cget('text')) == '▶  B'

    def test_header_menu(self, tk_root):
        """Header menu entries call their callbacks."""
        first, second = ConstantFn(), ConstantFn()
        frame = CollapsibleFrame(
            tk_root, text='M',
            header_menu_items=[('One', first), ('Two', second)])
        frame.pack()
        menu = find_widgets(frame._frame, tk.Menu)[0]
        menu.invoke(1)
        assert not first.calls
        assert len(second.calls) == 1
        assert find_widgets(frame._frame, ttk.Label, '⋯')

    def test_destroy(self, tk_root):
        """destroy() removes the wrapped frame."""
        frame = CollapsibleFrame(tk_root, text='Gone')
        frame.pack()
        frame.destroy()
        assert not frame._frame.winfo_exists()


# ---------------------------------------------------------------
# Layout
# ---------------------------------------------------------------

class TestLayout:
    """Scalar fields are laid out in tiers by count."""

    @pytest.mark.parametrize('count, size', [
        (3, (6, 1)),    # one row, label + widget per field
        (6, (6, 2)),    # three-column grid
        (9, (2, 9)),    # single column
    ])
    def test_grid_size(self, tk_root, count, size):
        """The grid has the expected (columns, rows)."""
        frame = ttk.Frame(tk_root)
        tree = build_fields_in_frame(
            frame, scalar_model_specs(count), max_horizontal=4)
        assert len(tree) == count
        assert frame.grid_size() == size

    def test_nested_after_scalars(self, tk_root):
        """Nested sections are placed in rows after the scalars."""
        frame = ttk.Frame(tk_root)
        tree = build_fields_in_frame(frame, introspect_model(Trip))
        assert tree['address'][0] == builder._MODEL_TAG
        assert tree['stops'][0] == builder._MODEL_LIST_TAG
        assert tree['tags'][0] == builder._SCALAR_LIST_TAG
        # 7 scalars with max_horizontal=4 -> 3 columns, 3 rows,
        # then 4 nested rows.
        assert frame.grid_size() == (6, 7)


# ---------------------------------------------------------------
# collect_values / load_values
# ---------------------------------------------------------------

class TestCollectLoad:
    """Values round-trip between dicts and widgets."""

    def test_defaults(self, tk_root):
        """A fresh form collects the model defaults."""
        values = collect_values(build_trip_tree(tk_root))
        assert values['speed'] == 9.5
        assert values['urgent'] is False
        assert values['transport'] == 'train'
        assert values['note'] is None
        assert values['out_file'] == 'out.csv'
        assert values['address'] == {'street': '',
                                     'city': 'Springfield'}
        assert values['stops'] == [{'miles': None, 'month': 'Dec'}]
        assert values['tags'] == ['x']
        assert values['sizes'] == []

    def test_round_trip(self, tk_root):
        """load_values then collect_values returns the same data."""
        tree = build_trip_tree(tk_root)
        load_values(tree, TRIP_DATA)
        values = collect_values(tree)
        assert Trip.model_validate(values) == Trip.model_validate(
            TRIP_DATA)
        assert values['stops'] == TRIP_DATA['stops']

    def test_partial_load(self, tk_root):
        """Keys missing from the data leave widgets unchanged."""
        tree = build_trip_tree(tk_root)
        load_values(tree, {'traveler': 'Bo', 'stops': 'not a list',
                           'tags': None})
        values = collect_values(tree)
        assert values['traveler'] == 'Bo'
        assert values['speed'] == 9.5
        assert len(values['stops']) == 1
        assert values['tags'] == ['x']

    def test_non_dict_ignored(self, tk_root):
        """load_values ignores data that is not a dict."""
        tree = build_trip_tree(tk_root)
        load_values(tree, ['traveler'])
        load_values(tree, {'address': None})
        assert collect_values(tree)['traveler'] == ''

    def test_none_clears_nullable(self, tk_root):
        """Loading None into a nullable field collects None."""
        tree = build_trip_tree(tk_root)
        load_values(tree, {'note': 'x'})
        load_values(tree, {'note': None})
        assert collect_values(tree)['note'] is None

    def test_empty_numbers_are_none(self, tk_root):
        """Blank int and float entries collect as None."""
        tree = build_trip_tree(tk_root)
        load_values(tree, {'seats': ' ', 'speed': ''})
        values = collect_values(tree)
        assert values['seats'] is None
        assert values['speed'] is None

    def test_bad_int_names_field(self, tk_root):
        """A non-integer entry raises ValueError naming the field."""
        tree = build_trip_tree(tk_root)
        load_values(tree, {'seats': 'lots'})
        with pytest.raises(ValueError, match='^For seats: '):
            collect_values(tree)

    def test_unexpected_error_propagates(self, tk_root):
        """Errors other than ValueError are re-raised unchanged."""
        tree = {'broken': ('not a spec', None)}
        with pytest.raises(AttributeError):
            collect_values(tree)

    @pytest.mark.xfail(strict=True, reason=(
        'collect_values builds nested paths innermost-first and '
        'wraps the message once per level'))
    def test_nested_error_path(self, tk_root):
        """A nested error names the path from the top model down."""
        frame = ttk.Frame(tk_root)
        tree = build_fields_in_frame(frame, introspect_model(Deep))
        load_values(tree, {'middle': {'inner': {'value': 'abc'}}})
        with pytest.raises(ValueError) as excinfo:
            collect_values(tree)
        message = str(excinfo.value)
        assert message.startswith('For middle/inner/value: ')
        assert message.count('For ') == 1


# ---------------------------------------------------------------
# Scalar-list and filepath widgets
# ---------------------------------------------------------------

class TestScalarList:
    """Tests for :class:`_ScalarListWidget`."""

    def make(self, tk_root, inner_type='int', default=''):
        """Build a scalar-list widget of *inner_type*."""
        spec = {'name': 'items', 'inner_type': inner_type,
                'default': default, 'help': 'Some items.'}
        return _ScalarListWidget(ttk.Frame(tk_root), spec)

    def test_collect_casts_and_skips_blanks(self, tk_root):
        """Lines are cast to the inner type; blank lines skipped."""
        widget = self.make(tk_root, 'float', '1.5\n\n  \n2')
        assert widget.collect() == [1.5, 2.0]

    def test_empty(self, tk_root):
        """An empty text area collects an empty list."""
        assert self.make(tk_root).collect() == []

    def test_bad_line_shows_error(self, tk_root):
        """A bad line raises and is shown in the error label."""
        widget = self.make(tk_root, 'int', '1\nx\n3')
        with pytest.raises(ValueError, match="Line 2: 'x'"):
            widget.collect()
        assert "Line 2: 'x'" in str(widget._error_label.cget('text'))
        widget.load([4, 5])
        assert widget.collect() == [4, 5]
        assert str(widget._error_label.cget('text')) == ''

    def test_validate(self, tk_root):
        """_validate reports bad lines and clears when fixed."""
        widget = self.make(tk_root, 'int', 'a\n2')
        errors = widget._validate()
        assert errors == ["Line 1: 'a' is not a valid int"]
        assert 'Line 1' in str(widget._error_label.cget('text'))
        widget.load(['7'])
        assert widget._validate() == []
        assert str(widget._error_label.cget('text')) == ''

    def test_load_non_list_clears(self, tk_root):
        """Loading something that is not a list empties the widget."""
        widget = self.make(tk_root, 'str', 'a')
        widget.load(None)
        assert widget.collect() == []


class TestFilepathWidget:
    """The Browse button fills the entry from a file dialog."""

    def browse_button(self, tk_root, mode):
        """Build a filepath field and return (button, var)."""
        frame = ttk.Frame(tk_root)
        spec = {'name': 'path', 'gui_type': 'filepath',
                'default': 'old.txt', 'mode': mode,
                'filetypes': [('Text', '*.txt')],
                'defaultextension': '.txt'}
        tree = build_fields_in_frame(frame, [spec])
        button = find_widgets(frame, ttk.Button, 'Browse…')[0]
        return button, tree['path'][1]

    def test_open_mode(self, tk_root, monkeypatch):
        """Open mode uses askopenfilename and stores its result."""
        fake = patch_dialog(monkeypatch, 'askopenfilename', '/a/b.txt')
        button, var = self.browse_button(tk_root, 'open')
        button.invoke()
        assert var.get() == '/a/b.txt'
        assert fake.calls[0][1]['filetypes'] == [('Text', '*.txt')]

    def test_save_mode(self, tk_root, monkeypatch):
        """Save mode passes the default extension to the dialog."""
        fake = patch_dialog(monkeypatch, 'asksaveasfilename', '/c.txt')
        button, var = self.browse_button(tk_root, 'save')
        button.invoke()
        assert var.get() == '/c.txt'
        assert fake.calls[0][1]['defaultextension'] == '.txt'

    def test_cancel_keeps_value(self, tk_root, monkeypatch):
        """Cancelling the dialog leaves the entry unchanged."""
        patch_dialog(monkeypatch, 'askopenfilename', '')
        button, var = self.browse_button(tk_root, 'open')
        button.invoke()
        assert var.get() == 'old.txt'


# ---------------------------------------------------------------
# Lists of sub-models
# ---------------------------------------------------------------

class TestModelListSection:
    """Tests for :class:`_ModelListSection`."""

    def make(self, tk_root):
        """Build a section of Stop entries."""
        return _ModelListSection(ttk.Frame(tk_root), Stop)

    def test_add_and_remove(self, tk_root):
        """+ Add and - Remove Last change the entry count."""
        section = self.make(tk_root)
        outer = section._outer
        add = find_widgets(outer, ttk.Button, '+ Add')[0]
        remove = find_widgets(outer, ttk.Button, '− Remove Last')[0]
        add.invoke()
        add.invoke()
        assert len(section.collect_all()) == 3
        assert str(section._count_label.cget('text')) == '(3 item(s))'
        remove.invoke()
        assert len(section.collect_all()) == 2

    def test_keeps_one_entry(self, tk_root):
        """Removing the last remaining entry is refused."""
        section = self.make(tk_root)
        section._remove_entry()
        assert len(section.collect_all()) == 1

    def test_load_all_replaces(self, tk_root):
        """load_all replaces all entries with the given items."""
        section = self.make(tk_root)
        section.load_all([{'miles': 1.0}, {'miles': 2.0,
                                            'month': 'Mar'}])
        assert section.collect_all() == [
            {'miles': 1.0, 'month': 'Dec'},
            {'miles': 2.0, 'month': 'Mar'},
        ]

    def test_dump_and_load_list(self, tk_root, monkeypatch, tmp_path):
        """The list menu dumps to JSON and loads it back."""
        path = tmp_path / 'stops.json'
        section = self.make(tk_root)
        section.load_all(TRIP_DATA['stops'])
        patch_dialog(monkeypatch, 'asksaveasfilename', str(path))
        section._dump_list()
        assert read_json(path) == TRIP_DATA['stops']

        other = self.make(tk_root)
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        other._load_list()
        assert other.collect_all() == TRIP_DATA['stops']

    def test_dump_invalid_writes_nothing(self, tk_root, monkeypatch,
                                         tmp_path):
        """A list that fails validation is not written."""
        path = tmp_path / 'stops.json'
        fake = patch_dialog(monkeypatch, 'asksaveasfilename', str(path))
        section = self.make(tk_root)   # miles is blank -> None
        section._dump_list()
        assert not fake.calls
        assert not path.exists()

    def test_dump_bad_input_writes_nothing(self, tk_root, monkeypatch):
        """A list with an unparsable number opens no dialog."""
        fake = patch_dialog(monkeypatch, 'asksaveasfilename', 'x')
        section = self.make(tk_root)
        load_values(section._entries[0][1], {'miles': 'high'})
        section._dump_list()
        assert not fake.calls

    def test_dump_cancel(self, tk_root, monkeypatch, tmp_path):
        """Cancelling the save dialog writes nothing."""
        patch_dialog(monkeypatch, 'asksaveasfilename', '')
        section = self.make(tk_root)
        section.load_all([{'miles': 1.0}])
        section._dump_list()
        assert not list(tmp_path.iterdir())

    def test_dump_file_error_logged(self, tk_root, monkeypatch, tmp_path,
                                    caplog):
        """A file that cannot be written is logged as an error."""
        bad = tmp_path / 'missing' / 'stops.json'
        patch_dialog(monkeypatch, 'asksaveasfilename', str(bad))
        section = self.make(tk_root)
        section.load_all([{'miles': 1.0}])
        with caplog.at_level(logging.ERROR):
            section._dump_list()
        assert 'List dump file error' in caplog.text

    @pytest.mark.parametrize('content, message', [
        ('{"miles": 1}', 'Expected a JSON array'),
        ('not json', 'List load error'),
    ])
    def test_load_rejects_bad_file(self, tk_root, monkeypatch, tmp_path,
                                   caplog, content, message):
        """A non-array or invalid JSON file leaves entries alone."""
        path = tmp_path / 'stops.json'
        path.write_text(content, encoding='utf-8')
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        section = self.make(tk_root)
        section.load_all([{'miles': 5.0}])
        with caplog.at_level(logging.ERROR):
            section._load_list()
        assert message in caplog.text
        assert section.collect_all() == [{'miles': 5.0,
                                          'month': 'Dec'}]

    def test_load_invalid_items_still_loads(self, tk_root, monkeypatch,
                                            tmp_path, caplog):
        """Items failing validation are logged but still loaded."""
        path = tmp_path / 'stops.json'
        write_json(path, [{'month': 'Apr'}])
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        section = self.make(tk_root)
        with caplog.at_level(logging.WARNING):
            section._load_list()
        assert 'List item validation warning' in caplog.text
        assert section.collect_all()[0]['month'] == 'Apr'

    def test_load_cancel(self, tk_root, monkeypatch):
        """Cancelling the open dialog changes nothing."""
        patch_dialog(monkeypatch, 'askopenfilename', '')
        section = self.make(tk_root)
        section.load_all([{'miles': 5.0}])
        section._load_list()
        assert section.collect_all()[0]['miles'] == 5.0


class TestSubModelDumpLoad:
    """Tests for the per-sub-model Dump / Load menu actions."""

    def make(self, tk_root, model=Address):
        """Return (holder, frame) for a form of *model*."""
        frame = ttk.Frame(tk_root)
        tree = build_fields_in_frame(frame, introspect_model(model))
        return {'tree': tree}, frame

    def test_round_trip(self, tk_root, monkeypatch, tmp_path):
        """A dumped sub-model loads back into another form."""
        path = tmp_path / 'addr.json'
        holder, frame = self.make(tk_root)
        load_values(holder['tree'], {'street': '2 Elm'})
        patch_dialog(monkeypatch, 'asksaveasfilename', str(path))
        _dump_sub_model(Address, holder, frame)
        assert read_json(path) == {'street': '2 Elm',
                                   'city': 'Springfield'}

        other, other_frame = self.make(tk_root)
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        _load_sub_model(Address, other, other_frame)
        assert collect_values(other['tree'])['street'] == '2 Elm'

    def test_without_tree_does_nothing(self, tk_root, monkeypatch):
        """With no widget tree yet, no dialog is opened."""
        save = patch_dialog(monkeypatch, 'asksaveasfilename', 'x')
        load = patch_dialog(monkeypatch, 'askopenfilename', 'x')
        _dump_sub_model(Address, {}, tk_root)
        _load_sub_model(Address, {}, tk_root)
        assert not save.calls and not load.calls

    def test_dump_errors_open_no_dialog(self, tk_root, monkeypatch,
                                        caplog):
        """Bad input or a validation error skips the save dialog."""
        save = patch_dialog(monkeypatch, 'asksaveasfilename', 'x')
        holder, frame = self.make(tk_root, Stop)
        with caplog.at_level(logging.ERROR):
            _dump_sub_model(Stop, holder, frame)
            load_values(holder['tree'], {'miles': 'high'})
            _dump_sub_model(Stop, holder, frame)
        assert 'Sub-model validation error' in caplog.text
        assert 'Sub-model dump error' in caplog.text
        assert not save.calls

    def test_dump_cancel_and_file_error(self, tk_root, monkeypatch,
                                        tmp_path, caplog):
        """Cancel writes nothing; an unwritable path is logged."""
        holder, frame = self.make(tk_root)
        patch_dialog(monkeypatch, 'asksaveasfilename', '')
        _dump_sub_model(Address, holder, frame)
        assert not list(tmp_path.iterdir())
        bad = tmp_path / 'missing' / 'a.json'
        patch_dialog(monkeypatch, 'asksaveasfilename', str(bad))
        with caplog.at_level(logging.ERROR):
            _dump_sub_model(Address, holder, frame)
        assert 'Sub-model file error' in caplog.text

    def test_load_bad_file_and_warning(self, tk_root, monkeypatch,
                                       tmp_path, caplog):
        """Bad JSON is logged; invalid data is warned but loaded."""
        holder, frame = self.make(tk_root)
        bad = tmp_path / 'bad.json'
        bad.write_text('{', encoding='utf-8')
        patch_dialog(monkeypatch, 'askopenfilename', str(bad))
        with caplog.at_level(logging.WARNING):
            _load_sub_model(Address, holder, frame)
        assert 'Sub-model load error' in caplog.text

        partial = tmp_path / 'partial.json'
        write_json(partial, {'city': 'Ogdenville'})
        patch_dialog(monkeypatch, 'askopenfilename', str(partial))
        with caplog.at_level(logging.WARNING):
            _load_sub_model(Address, holder, frame)
        assert 'Sub-model validation warning' in caplog.text
        assert collect_values(holder['tree'])['city'] == 'Ogdenville'

    def test_load_cancel(self, tk_root, monkeypatch):
        """Cancelling the open dialog leaves the form alone."""
        holder, frame = self.make(tk_root)
        patch_dialog(monkeypatch, 'askopenfilename', '')
        _load_sub_model(Address, holder, frame)
        assert collect_values(holder['tree'])['city'] == 'Springfield'

    def test_nested_frame_menu(self, tk_root, monkeypatch, tmp_path):
        """The nested model's header menu dumps that sub-model."""
        path = tmp_path / 'addr.json'
        frame = ttk.Frame(tk_root)
        tree = build_fields_in_frame(frame, introspect_model(Trip))
        load_values(tree, {'address': {'street': '9 Oak'}})
        patch_dialog(monkeypatch, 'asksaveasfilename', str(path))
        menus = find_widgets(frame, tk.Menu)
        address_menu = [
            m for m in menus
            if m.entrycget(1, 'label') == 'Dump Address to JSON…'
        ][0]
        address_menu.invoke(1)
        assert read_json(path)['street'] == '9 Oak'

    def test_entry_frame_menu(self, tk_root, monkeypatch, tmp_path):
        """A list entry's header menu loads into that entry only."""
        path = tmp_path / 'stop.json'
        write_json(path, {'miles': 3.0, 'month': 'May'})
        section = _ModelListSection(ttk.Frame(tk_root), Stop)
        section._add_entry()
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        second_frame = section._entries[1][0]
        menu = find_widgets(second_frame._frame, tk.Menu)[0]
        menu.invoke(0)
        assert section.collect_all() == [
            {'miles': None, 'month': 'Dec'},
            {'miles': 3.0, 'month': 'May'},
        ]


class TestScrollableCanvas:
    """Tests for :func:`setup_scrollable_canvas`."""

    def test_scroll(self, tk_root):
        """Tall content gets a scroll region and wheel events scroll."""
        top = tk.Toplevel(tk_root)
        top.geometry('200x100')
        canvas, inner = setup_scrollable_canvas(top)
        for i in range(40):
            ttk.Label(inner, text=f'row {i}').pack()
        top.update()
        assert canvas.cget('scrollregion') != ''
        assert canvas.yview()[0] == 0.0
        canvas.event_generate('<Button-5>')
        assert canvas.yview()[0] > 0.0
        canvas.event_generate('<Button-4>')
        assert canvas.yview()[0] == 0.0
        canvas.event_generate('<MouseWheel>', delta=-120)
        assert canvas.yview()[0] > 0.0


# ---------------------------------------------------------------
# ModelCommandWindow
# ---------------------------------------------------------------

class SubmitRecorder:
    """Command callback that records the model it receives."""

    def __init__(self, result='done'):
        """Remember the value to return."""
        self.result = result
        self.trips = []

    def __call__(self, trip):
        """Record *trip* and return the fixed result."""
        self.trips.append(trip)
        return self.result


class FakeRPCCall:
    """Stand-in for SimpleRPCCall that finishes synchronously."""

    instances = []

    def __init__(self, url, command_name, cmd_args, rpc_timeout):
        """Record the call arguments."""
        self.url = url
        self.command_name = command_name
        self.cmd_args = cmd_args
        self.rpc_timeout = rpc_timeout
        self.after = None
        self.result = 'rpc says hi'
        FakeRPCCall.instances.append(self)

    def start(self):
        """Record the Submit state, then run the after callback."""
        window = self.after.function.__self__
        self.submit_state = str(window._submit_btn.cget('state'))
        self.after.function(*self.after.args)


def make_window(tk_root, callback=None, model=Small, url=''):
    """Build a ModelCommandWindow for *model*."""
    command = Command(title='Do It', parameters=[model],
                      description='Test command.', callback=callback)
    url_var = tk.StringVar(tk_root, value=url)
    timeout_var = tk.IntVar(tk_root, value=7)
    return ModelCommandWindow(tk_root, command=command,
                              url_var=url_var, timeout_var=timeout_var)


def result_of(window):
    """Return the text shown in the window's result pane."""
    return text_content(window._result_text)


class TestModelCommandWindow:
    """Tests for :class:`ModelCommandWindow`."""

    def test_title_and_fields(self, tk_root):
        """The window is titled after the command and has fields."""
        window = make_window(tk_root, model=Trip)
        assert window.title() == 'Do It'
        assert set(window._widget_tree) == set(Trip.model_fields)

    def test_submit_calls_callback(self, tk_root):
        """Submit validates and passes the model to the callback."""
        recorder = SubmitRecorder('all good')
        window = make_window(tk_root, recorder, model=Trip)
        load_values(window._widget_tree, TRIP_DATA)
        window._submit_btn.invoke()
        assert recorder.trips == [Trip.model_validate(TRIP_DATA)]
        assert result_of(window) == 'all good'

    def test_non_string_result(self, tk_root):
        """A non-string callback result is shown with str()."""
        window = make_window(tk_root, SubmitRecorder(42))
        window._submit_btn.invoke()
        assert result_of(window) == '42'

    def test_validation_error_shown(self, tk_root):
        """A validation error is shown and the callback not called."""
        recorder = SubmitRecorder()
        window = make_window(tk_root, recorder, model=Trip)
        window._submit_btn.invoke()   # seats and miles blank
        assert result_of(window).startswith('VALIDATION ERROR:')
        assert 'seats' in result_of(window)
        assert not recorder.trips

    def test_input_error_shown(self, tk_root):
        """An unparsable number is shown as an input error."""
        recorder = SubmitRecorder()
        window = make_window(tk_root, recorder, model=Trip)
        load_values(window._widget_tree, {'seats': 'many'})
        window._submit_btn.invoke()
        assert result_of(window).startswith(
            'INPUT ERROR: For seats:')
        assert not recorder.trips

    def test_empty_url(self, tk_root):
        """Without a callback or server URL, an error is shown."""
        window = make_window(tk_root, url='  ')
        window._submit_btn.invoke()
        assert result_of(window) == 'ERROR: Server URL is empty.'

    def test_rpc_submit(self, tk_root, monkeypatch):
        """Without a callback, the command is sent over XML-RPC."""
        monkeypatch.setattr(FakeRPCCall, 'instances', [])
        monkeypatch.setattr(builder, 'SimpleRPCCall', FakeRPCCall)
        window = make_window(tk_root, url='http://127.0.0.1:1')
        load_values(window._widget_tree, {'name': 'Zed'})
        window._submit_btn.invoke()
        call = FakeRPCCall.instances[0]
        assert call.url == 'http://127.0.0.1:1'
        assert call.command_name == 'do_it'
        assert call.cmd_args == Small(name='Zed')
        assert call.rpc_timeout == 7
        assert call.submit_state == 'disabled'
        assert result_of(window) == 'rpc says hi'
        assert str(window._submit_btn.cget('state')) == 'normal'

    def test_dump_and_load(self, tk_root, monkeypatch, tmp_path):
        """Dump writes the validated model; Load fills a new form."""
        path = tmp_path / 'trip.json'
        window = make_window(tk_root, model=Trip)
        load_values(window._widget_tree, TRIP_DATA)
        patch_dialog(monkeypatch, 'asksaveasfilename', str(path))
        find_widgets(window, ttk.Button, 'Dump')[0].invoke()
        assert result_of(window) == f'Saved to {path}'
        expected = Trip.model_validate(TRIP_DATA)
        assert Trip.model_validate(read_json(path)) == expected

        other = make_window(tk_root, model=Trip)
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        find_widgets(other, ttk.Button, 'Load')[0].invoke()
        assert result_of(other) == f'Loaded from {path}'
        loaded = collect_values(other._widget_tree)
        assert Trip.model_validate(loaded) == expected

    def test_dump_errors(self, tk_root, monkeypatch):
        """Dump shows input and validation errors without saving."""
        save = patch_dialog(monkeypatch, 'asksaveasfilename', 'x')
        window = make_window(tk_root, model=Trip)
        window._on_dump()
        assert result_of(window).startswith('VALIDATION ERROR:')
        load_values(window._widget_tree, {'speed': 'fast'})
        window._on_dump()
        assert result_of(window).startswith('INPUT ERROR:')
        assert not save.calls

    def test_dump_cancel_and_file_error(self, tk_root, monkeypatch,
                                        tmp_path):
        """Cancel saves nothing; a bad path shows a file error."""
        window = make_window(tk_root)
        patch_dialog(monkeypatch, 'asksaveasfilename', '')
        window._on_dump()
        assert result_of(window) == ''
        bad = tmp_path / 'missing' / 'x.json'
        patch_dialog(monkeypatch, 'asksaveasfilename', str(bad))
        window._on_dump()
        assert result_of(window).startswith('FILE ERROR:')

    def test_load_cancel_and_bad_json(self, tk_root, monkeypatch,
                                      tmp_path):
        """Cancel changes nothing; bad JSON shows a load error."""
        window = make_window(tk_root)
        patch_dialog(monkeypatch, 'askopenfilename', '')
        window._on_load()
        assert result_of(window) == ''
        bad = tmp_path / 'bad.json'
        bad.write_text('[', encoding='utf-8')
        patch_dialog(monkeypatch, 'askopenfilename', str(bad))
        window._on_load()
        assert result_of(window).startswith('LOAD ERROR:')

    def test_load_invalid_still_fills(self, tk_root, monkeypatch,
                                      tmp_path):
        """Loading data that fails validation still fills the form."""
        path = tmp_path / 'partial.json'
        write_json(path, {'traveler': 'Cy'})
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        window = make_window(tk_root, model=Trip)
        window._on_load()
        assert collect_values(window._widget_tree)['traveler'] == 'Cy'

    @pytest.mark.xfail(strict=True, reason=(
        '_on_load overwrites the VALIDATION WARNING with '
        '"Loaded from ..." so the warning is never seen'))
    def test_load_invalid_shows_warning(self, tk_root, monkeypatch,
                                        tmp_path):
        """Loading data that fails validation keeps the warning shown."""
        path = tmp_path / 'partial.json'
        write_json(path, {'traveler': 'Cy'})
        patch_dialog(monkeypatch, 'askopenfilename', str(path))
        window = make_window(tk_root, model=Trip)
        window._on_load()
        assert 'VALIDATION WARNING' in result_of(window)


# ---------------------------------------------------------------
# MainApp
# ---------------------------------------------------------------

class ParentRecorder:
    """Callback for a parameterless command; records ``parent``."""

    def __init__(self):
        """Start with no recorded parents."""
        self.parents = []

    def __call__(self, parent):
        """Record the parent window."""
        self.parents.append(parent)


MAIN_COMMANDS = {
    'small': Command(title='Make Small', parameters=[Small],
                     description='Takes a Small model.'),
    'plain': Command(title='Plain', description='No parameters.'),
    'double': Command(title='Double', parameters=[Small, Stop],
                      description='Two models.'),
}


@pytest.fixture
def main_app(monkeypatch):
    """Yield a withdrawn MainApp, or skip without a display."""
    recorder = ParentRecorder()
    commands = dict(MAIN_COMMANDS)
    commands['plain'] = commands['plain'].model_copy(
        update={'callback': recorder})
    try:
        app = MainApp('Test App', commands, bind='192.0.2.1', port=99)
    except tk.TclError as exc:
        pytest.skip(f'No display available for Tk: {exc}')
    app.withdraw()
    app.recorder = recorder
    yield app
    app.destroy()


class TestMainApp:
    """Tests for :class:`MainApp`."""

    def test_default_url(self):
        """The default URL uses 127.0.0.1:8765 unless overridden."""
        assert MainApp.get_default_server_url() == (
            'http://127.0.0.1:8765')
        assert MainApp.get_default_server_url('h', 1) == 'http://h:1'

    def test_construction(self, main_app):
        """The app shows the URL, timeout and one button per command."""
        assert main_app.title() == 'Test App'
        assert main_app._url_var.get() == 'http://192.0.2.1:99'
        assert main_app._timeout_var.get() == 20
        labels = [str(b.cget('text'))
                  for b in find_widgets(main_app, ttk.Button)]
        assert labels == ['Make Small', 'Plain', 'Double']

    def test_button_opens_window(self, main_app):
        """A command with a model opens a ModelCommandWindow."""
        find_widgets(main_app, ttk.Button, 'Make Small')[0].invoke()
        windows = find_widgets(main_app, ModelCommandWindow)
        assert len(windows) == 1
        assert windows[0].model_class is Small
        assert windows[0]._server_config['url_var'] is (
            main_app._url_var)

    def test_plain_command_runs(self, main_app):
        """A command without parameters calls its callback."""
        find_widgets(main_app, ttk.Button, 'Plain')[0].invoke()
        assert main_app.recorder.parents == [main_app]

    def test_two_parameters_rejected(self, main_app):
        """A command with two parameter models is refused."""
        with pytest.raises(ValueError, match='parameters !=1'):
            main_app._open_command(MAIN_COMMANDS['double'])
