"""Lightweight status display widget for tkinter.

Provides :class:`SimpleStatusDisplay`, a ``tk.Toplevel`` window
that periodically calls a user-supplied callable and displays
the resulting status tuples in a scrollable text area.  Heading
lines (the first element of each tuple) are automatically
rendered in bold.

Structured status data
~~~~~~~~~~~~~~~~~~~~~~

The updater callable may return plain string tuples (for
backward compatibility) or :class:`StatusEntry` objects for
richer behaviour.

A :class:`StatusEntry` groups a heading with a sequence of
:class:`StatusField` rows.  Each ``StatusField`` carries the
display *text* and an optional *tooltip* that is shown when the
user hovers over that line in the display.

Plain tuples are automatically normalised to ``StatusEntry``
objects, so existing updaters continue to work unchanged.

Example::

    from ox_ui.tkdantic.status_display import (
        SimpleStatusDisplay, StatusEntry, StatusField,
    )

    def my_updater():
        return [
            StatusEntry(
                heading="Server",
                rows=(
                    StatusField("status: running"),
                    StatusField(
                        "uptime: 3h 22m",
                        tooltip="Started at 2025-03-26 06:38 UTC",
                    ),
                ),
            ),
            # Plain tuples still work:
            ("Database", "status: ok", "connections: 5"),
        ]

Hover-pause
~~~~~~~~~~~

When the mouse pointer enters the status text area, automatic
updates are suppressed so that tooltips and highlighted text
remain stable.  Only the most recent update is kept; it is
applied as soon as the mouse leaves the text area.  A small
indicator in the footer shows when hover-pause is active.

Clicking the **Refresh** button always clears the hover-pause
state and forces an immediate update, which acts as a
reliable escape hatch if things get out of sync.

Regex-based highlighting
~~~~~~~~~~~~~~~~~~~~~~~~

The ``highlights`` parameter accepts a list of
``(pattern, tag_config)`` tuples.  Each *pattern* is a regular
expression string; *tag_config* is a dict of keyword arguments
passed to ``tk.Text.tag_configure``.  Patterns are matched
using ``re.search`` (via ``re.finditer``), so they can match a
substring anywhere in a line — the entire line need not match.

If the pattern contains a **capturing group**, only the text
matched by group 1 is highlighted — the rest of the pattern
acts as context.  This lets you condition highlighting on the
surrounding line content without colouring the entire match.
Patterns with no groups behave as before: the full match is
highlighted.

When two patterns match overlapping text, the **first** entry
in the ``highlights`` list wins (highest priority).  The
auto-bold heading tag always has the *lowest* priority, so
highlight colours layer on top of bold headings without
conflict.

Commonly used *tag_config* keys
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``foreground`` / ``background``
    Foreground (text) or background colour.  Any Tk colour
    string works: named colours (``"red"``, ``"dodger blue"``),
    hex (``"#ff0000"``), or RGB (``"#rgb"``).

``font``
    A Tk font descriptor, e.g. ``("Consolas", 10, "bold")`` or
    ``("Consolas", 10, "bold italic")``.

``underline``
    Boolean — ``True`` draws an underline.

``overstrike``
    Boolean — ``True`` draws a strikethrough.

``relief``
    Border relief style around the tagged text.  One of
    ``"flat"``, ``"raised"``, ``"sunken"``, ``"groove"``,
    ``"ridge"``.  Pair with ``borderwidth`` to control
    thickness.

``lmargin1`` / ``lmargin2``
    Left indent (in pixels) for the first / subsequent wrapped
    lines of a paragraph.

``spacing1`` / ``spacing3``
    Extra vertical space (in pixels) above / below the line.

Click callbacks
^^^^^^^^^^^^^^^

A highlight rule may include an optional third element — a
callable that is invoked when the user clicks on tagged text.
The callable receives two positional arguments:

``line_text``
    The full text of the line that was clicked.

``match_text``
    The specific tagged substring that was clicked (i.e. the
    highlighted portion, which may be a capturing-group
    sub-match rather than the whole regex match).

When a tag has a callback the mouse cursor automatically
changes to a hand pointer on hover to signal interactivity.
If multiple clickable tags overlap, the highest-priority
tag's callback fires (first rule wins, same as for colours).

Example usage::

    import tkinter as tk
    from ox_ui.tkdantic.status_display import SimpleStatusDisplay

    def my_updater():
        return [
            ("Server", "status: running", "uptime: 3h 22m"),
            ("Database", "status: error", "connections: 0"),
        ]

    highlights = [
        # The word "error" in red, case-insensitive.
        (r"(?i)error",   {"foreground": "red"}),
        # "running" in green wherever it appears in a line.
        (r"running",     {"foreground": "green"}),
        # Highlight numeric values with a yellow background.
        (r"\\d+",         {"background": "lightyellow"}),
        # Bold + blue for anything matching "Server".
        (r"Server",      {"foreground": "blue",
                          "font": ("Consolas", 10, "bold")}),
        # Underline any time value like "3h 22m".
        (r"\\d+h \\d+m",   {"underline": True}),

        # --- Capturing group examples ---
        # Highlight only the number after "connections: "
        # with a blue background, but only when it is
        # non-zero.  The full pattern matches for context;
        # only group 1 (the digits) gets tagged.
        (r"connections: ([1-9]\\d*)",
         {"background": "lightblue"}),
        # Highlight a non-zero fill count on a
        # "fills / cancels / orders:" line.
        (r"fills / cancels / orders: ([1-9]\\d*)",
         {"background": "lightblue"}),
        # Highlight the value after "status: " only when
        # the line also contains "Database".
        (r"Database.*status: (\\S+)",
         {"foreground": "orange"}),

        # --- Click callback examples ---
        # Make "error" clickable; print the full line and
        # the matched word when clicked.
        (r"(?i)error",
         {"foreground": "red", "underline": True},
         lambda line, match: print(
             f"Clicked '{match}' on line: {line}"
         )),
        # Click a non-zero connection count to trigger
        # a custom action with full line context.
        (r"connections: ([1-9]\\d*)",
         {"background": "lightblue"},
         lambda line, match: print(
             f"Connection count {match} clicked"
         )),
    ]

    root = tk.Tk()
    root.withdraw()
    display = SimpleStatusDisplay(
        root,
        title="My Status",
        updater=my_updater,
        highlights=highlights,
    )
    root.mainloop()
"""

import logging
import re
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk, scrolledtext
from typing import Any, Callable, Dict, List, NamedTuple, Tuple

from ox_ui.tkdantic.builder import add_tooltip

LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Structured status types
# ------------------------------------------------------------------

class StatusField(NamedTuple):
    """A single detail line within a status entry.

    Attributes
    ----------
    text :
        The display string (rendered with a four-space indent).
    tooltip :
        Optional tooltip text shown on hover.  ``None`` means
        no tooltip for this line.
    """

    text: str
    tooltip: str | None = None


class StatusEntry(NamedTuple):
    """A status section: a heading with detail rows.

    Attributes
    ----------
    heading :
        Bold heading text for the section.
    rows :
        Detail rows displayed beneath the heading.
    heading_tooltip :
        Optional tooltip text for the heading line itself.
    """

    heading: str
    rows: tuple[StatusField, ...] = ()
    heading_tooltip: str | None = None


# Type alias for the updater return value.
# Accepts plain string-tuples (backward compat) or StatusEntry.
StatusList = List[Tuple[str, ...] | StatusEntry]

# Type alias for a single highlight rule.
# Two-element form: (pattern, tag_config)
# Three-element form: (pattern, tag_config, on_click)
HighlightRule = (
    Tuple[str, Dict[str, Any]]
    | Tuple[str, Dict[str, Any], Callable[[str, str], Any]]
)

# Separator drawn between status items.
_SEPARATOR = "-" * 40

# Tag name for auto-bold headings.
_HEADING_TAG = "_heading"

# Delay (ms) before a tooltip appears on hover.
_TOOLTIP_DELAY_MS = 300


class SimpleStatusDisplay(tk.Toplevel):
    """Toplevel window that polls a callable and shows status.

    Parameters
    ----------
    master :
        Parent tkinter widget (typically the root window).
    title :
        Window title.
    updater :
        Callable taking no arguments and returning a list of
        string tuples **or** :class:`StatusEntry` objects.
        Plain tuples are normalised automatically.  The first
        element of each tuple is treated as a heading; the
        rest are indented detail lines.
    update_interval :
        Seconds between automatic refreshes (default 0.2).
    highlights :
        Optional list of highlight rules.  Each rule is a
        tuple of two or three elements:
        ``(pattern, tag_config)`` or
        ``(pattern, tag_config, on_click)``.
        *pattern* is a regex string matched with
        ``re.finditer`` (substring matches are supported).
        If the pattern contains a capturing group, only
        group 1 is highlighted; the rest of the pattern
        serves as context.  *tag_config* is a dict of
        ``tk.Text.tag_configure`` keyword arguments (e.g.
        ``foreground``, ``background``, ``font``,
        ``underline``).  *on_click*, when provided, is a
        callable with signature ``(line_text, match_text)``
        that fires when the user clicks on tagged text.
        The cursor changes to a hand pointer on hover for
        clickable tags.  Earlier entries in the list take
        priority when patterns overlap.
    """

    def __init__(
        self,
        master,
        *,
        title: str,
        updater: Callable[[], StatusList],
        update_interval: float = 0.2,
        highlights: List[HighlightRule] | None = None,
    ):
        """Initialise the display, build the UI, start polling."""
        super().__init__(master)
        self.title(title)
        self.minsize(500, 400)

        self._updater = updater
        self._last_rendered = ""
        self._paused = False
        self._highlights = self._compile_highlights(
            highlights or [],
        )

        # Hover-pause state: when the mouse is over the text
        # area we suppress redraws and stash the latest result.
        self._hover_paused = False
        self._pending_status: StatusList | None = None

        # Tooltip state.
        self._line_tooltips: dict[int, str] = {}
        self._tooltip_win: tk.Toplevel | None = None
        self._tooltip_after_id: str | None = None
        self._current_tooltip_line: int | None = None

        self._init_threading()
        self._build_controls(update_interval)
        self._build_status_area()
        self._configure_tags()
        self._build_footer()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_polling()

    # ----------------------------------------------------------
    # Entry normalisation
    # ----------------------------------------------------------

    @staticmethod
    def _normalize_entry(
        entry: tuple[str, ...] | StatusEntry,
    ) -> StatusEntry:
        """Convert a plain tuple to a :class:`StatusEntry`.

        If *entry* is already a ``StatusEntry`` it is returned
        unchanged.
        """
        if isinstance(entry, StatusEntry):
            return entry
        heading = entry[0] if entry else ""
        rows = tuple(StatusField(text=s) for s in entry[1:]) if entry[
            1:] else ()
        
        return StatusEntry(heading=heading, rows=rows)

    # ----------------------------------------------------------
    # Highlight compilation and tag setup
    # ----------------------------------------------------------

    # Compiled highlight entry: pattern, tag name, config, callback.
    _CompiledHL = tuple[
        re.Pattern, str, dict[str, Any],
        Callable[[str, str], Any] | None,
    ]

    @staticmethod
    def _compile_highlights(
        rules: List[HighlightRule],
    ) -> list["SimpleStatusDisplay._CompiledHL"]:
        """Compile regex patterns and assign tag names.

        Returns a list of ``(compiled_pattern, tag_name,
        tag_config, on_click)`` tuples.  *on_click* is
        ``None`` when the rule has no callback.
        """
        compiled: list[SimpleStatusDisplay._CompiledHL] = []
        for idx, rule in enumerate(rules):
            pattern = rule[0]
            config = rule[1]
            on_click = rule[2] if len(rule) > 2 else None
            tag_name = f"_hl_{idx}"
            compiled.append(
                (re.compile(pattern), tag_name, config, on_click),
            )
        return compiled

    def _configure_tags(self):
        """Configure the heading and highlight tags.

        The heading tag gets the lowest priority and each
        highlight tag is raised so that the first entry in
        the *highlights* list has the highest priority.
        Tags with an *on_click* callback get a click binding
        and a hand-pointer cursor on hover.
        """
        # Heading tag: bold variant of the base font.
        self._text.tag_configure(
            _HEADING_TAG, font=("Consolas", 10, "bold"),
        )

        # Store the default cursor so hover-leave restores it.
        self._default_cursor = self._text.cget("cursor")

        for _pattern, tag_name, config, on_click in (
            self._highlights
        ):
            self._text.tag_configure(tag_name, **config)
            if on_click is not None:
                self._bind_click(tag_name, on_click)

        # Set priority: heading lowest, then highlights in
        # reverse order so index 0 ends up highest.
        self._text.tag_lower(_HEADING_TAG)
        for _pattern, tag_name, _config, _cb in reversed(
            self._highlights
        ):
            self._text.tag_raise(tag_name)

    def _bind_click(
        self,
        tag_name: str,
        on_click: Callable[[str, str], Any],
    ):
        """Bind click and cursor events for a clickable tag.

        On click the callback receives ``(line_text,
        match_text)`` where *line_text* is the full text of
        the clicked line and *match_text* is the tagged
        substring that was clicked.
        """

        def _handler(event, _cb=on_click, _tag=tag_name):
            self._on_tag_click(event, _cb, _tag)

        self._text.tag_bind(
            tag_name, "<Button-1>", _handler,
        )
        self._text.tag_bind(
            tag_name,
            "<Enter>",
            lambda _e: self._text.config(cursor="hand2"),
        )
        self._text.tag_bind(
            tag_name,
            "<Leave>",
            lambda _e: self._text.config(
                cursor=self._default_cursor,
            ),
        )

    def _on_tag_click(
        self,
        event: tk.Event,
        callback: Callable[[str, str], Any],
        tag_name: str,
    ):
        """Dispatch a click on tagged text to the callback.

        Resolves the full line text and the specific tagged
        substring under the cursor, then calls
        ``callback(line_text, match_text)``.
        """
        # Index under the mouse, e.g. "4.12".
        index = self._text.index(f"@{event.x},{event.y}")
        line_no = index.split(".")[0]
        line_text = self._text.get(
            f"{line_no}.0", f"{line_no}.end",
        )

        # Find the tagged range containing the click.
        # tag_prevrange looks backwards from the given index
        # (exclusive), so we step one character forward.
        tag_range = self._text.tag_prevrange(
            tag_name, f"{index}+1c",
        )
        if tag_range:
            match_text = self._text.get(*tag_range)
        else:
            match_text = ""

        try:
            callback(line_text, match_text)
        except Exception:
            LOGGER.exception(
                "on_click callback for tag %r raised an "
                "exception",
                tag_name,
            )

    def _init_threading(self):
        """Initialise the stop and pause threading events."""
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # start un-paused

    def _start_polling(self):
        """Launch the background polling thread."""
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
        )
        self._thread.start()

    # ----------------------------------------------------------
    # UI construction
    # ----------------------------------------------------------

    def _build_controls(self, default_interval: float):
        """Create the controls frame with interval and buttons."""
        frame = ttk.LabelFrame(
            self, text="Controls", padding=6,
        )
        frame.pack(fill="x", padx=8, pady=(8, 4))

        self._add_interval_entry(frame, default_interval)
        self._add_control_buttons(frame)

    def _add_interval_entry(self, parent, default: float):
        """Add the interval label and entry to *parent*."""
        ttk.Label(parent, text="Interval (s):").pack(
            side="left",
        )
        self._interval_var = tk.DoubleVar(value=default)
        entry = ttk.Entry(
            parent,
            textvariable=self._interval_var,
            width=8,
        )
        entry.pack(side="left", padx=(4, 8))
        add_tooltip(
            entry,
            "Seconds between automatic status refreshes.",
        )

    def _add_control_buttons(self, parent):
        """Add Refresh and Pause buttons to *parent*."""
        refresh_btn = ttk.Button(
            parent, text="Refresh",
            command=self._do_refresh,
        )
        refresh_btn.pack(side="left", padx=(0, 4))
        add_tooltip(
            refresh_btn, "Force an immediate refresh.",
        )

        self._pause_btn = ttk.Button(
            parent, text="Pause",
            command=self._toggle_pause,
        )
        self._pause_btn.pack(side="left", padx=(0, 4))
        add_tooltip(
            self._pause_btn,
            "Pause or resume automatic updates.",
        )

    def _build_status_area(self):
        """Create the scrollable text area for status output."""
        frame = ttk.LabelFrame(
            self, text="Status", padding=6,
        )
        frame.pack(
            fill="both", expand=True, padx=8, pady=(4, 4),
        )

        self._text = scrolledtext.ScrolledText(
            frame,
            height=16,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
        )
        self._text.pack(fill="both", expand=True)

        # Hover-pause: suppress redraws while inspecting.
        self._text.bind("<Enter>", self._on_text_enter)
        self._text.bind("<Leave>", self._on_text_leave)
        self._text.bind("<Motion>", self._on_text_motion)

    def _build_footer(self):
        """Create the footer with the last-updated timestamp."""
        footer = ttk.Frame(self, padding=(8, 0, 8, 6))
        footer.pack(fill="x")

        self._timestamp_label = ttk.Label(
            footer, text="Last updated: --",
        )
        self._timestamp_label.pack(side="left")

        self._hover_label = ttk.Label(
            footer, text="", foreground="gray",
        )
        self._hover_label.pack(side="left", padx=(8, 0))

    # ----------------------------------------------------------
    # Hover-pause
    # ----------------------------------------------------------

    def _on_text_enter(self, _event: tk.Event):
        """Mouse entered the text area — activate hover-pause.

        Only activates when the display is in running mode
        (not explicitly paused via the Pause button), since
        an explicit pause already suppresses updates.
        """
        if not self._paused:
            self._hover_paused = True
            self._update_hover_indicator()

    def _on_text_leave(self, _event: tk.Event):
        """Mouse left the text area — deactivate hover-pause.

        Applies the most recent pending status update (if
        any) so the display catches up immediately.
        """
        self._hide_tooltip()
        self._cancel_tooltip_timer()
        self._current_tooltip_line = None

        if not self._hover_paused:
            return

        self._hover_paused = False
        self._update_hover_indicator()

        if self._pending_status is not None:
            status = self._pending_status
            self._pending_status = None
            self._apply_status(status)

    def _update_hover_indicator(self):
        """Show or clear the hover-pause indicator in the footer."""
        if self._hover_paused:
            self._hover_label.config(
                text="\u23f8 updates paused (hovering)",
            )
        else:
            self._hover_label.config(text="")

    # ----------------------------------------------------------
    # Tooltip display
    # ----------------------------------------------------------

    def _on_text_motion(self, event: tk.Event):
        """Track the mouse and schedule or dismiss tooltips.

        When the cursor moves to a line that has tooltip text
        in ``_line_tooltips``, a timer is started.  If the
        cursor stays on the same line for
        :data:`_TOOLTIP_DELAY_MS` the tooltip is shown.
        Moving to a different line (or one with no tooltip)
        hides any visible tooltip immediately.
        """
        index = self._text.index(f"@{event.x},{event.y}")
        line_no = int(index.split(".")[0])

        # Still on the same line — nothing to do.
        if line_no == self._current_tooltip_line:
            return

        # Moving to a new line: cancel pending timer, hide tip.
        self._cancel_tooltip_timer()
        self._hide_tooltip()
        self._current_tooltip_line = None

        tooltip_text = self._line_tooltips.get(line_no)
        if tooltip_text is not None:
            x_root = event.x_root
            y_root = event.y_root
            self._tooltip_after_id = self.after(
                _TOOLTIP_DELAY_MS,
                self._show_tooltip,
                tooltip_text, line_no,
                x_root + 15, y_root + 10,
            )

    def _show_tooltip(
        self, text: str, line_no: int, x: int, y: int,
    ):
        """Display a tooltip window at the given screen coords."""
        self._tooltip_after_id = None
        self._current_tooltip_line = line_no
        self._hide_tooltip()  # ensure no stale window

        win = tk.Toplevel(self)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            win,
            text=text,
            background="#ffffe0",
            foreground="black",
            relief="solid",
            borderwidth=1,
            font=("Consolas", 9),
            justify="left",
            padx=6,
            pady=4,
            wraplength=400,
        )
        label.pack()

        self._tooltip_win = win

    def _hide_tooltip(self):
        """Destroy the tooltip window if it exists."""
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except tk.TclError:
                pass
            self._tooltip_win = None

    def _cancel_tooltip_timer(self):
        """Cancel any pending tooltip-show timer."""
        if self._tooltip_after_id is not None:
            self.after_cancel(self._tooltip_after_id)
            self._tooltip_after_id = None

    # ----------------------------------------------------------
    # Button callbacks
    # ----------------------------------------------------------

    def _toggle_pause(self):
        """Toggle between paused and running states."""
        if self._paused:
            self._paused = False
            self._pause_event.set()
            self._pause_btn.config(text="Pause")
        else:
            self._paused = True
            self._pause_event.clear()
            self._pause_btn.config(text="Resume")

    def _do_refresh(self):
        """Force a single synchronous refresh of the display.

        Always clears hover-pause state and discards any
        pending update so the user gets a predictable
        "show me what's happening right now" result.
        """
        self._hover_paused = False
        self._pending_status = None
        self._hide_tooltip()
        self._cancel_tooltip_timer()
        self._current_tooltip_line = None
        self._update_hover_indicator()

        status = self._fetch_status()

        if status is not None:
            self._apply_status(status)

    # ----------------------------------------------------------
    # Data fetching
    # ----------------------------------------------------------

    def _fetch_status(self) -> StatusList | None:
        """Call the updater and return its result.

        Returns ``None`` and logs the error if the updater
        raises an exception.
        """
        try:
            result = self._updater()
        except Exception as exc:
            LOGGER.exception(
                "Updater raised an exception: %s", exc,
            )
            self._show_error(str(exc))
            result = None
        return result

    # ----------------------------------------------------------
    # Rendering
    # ----------------------------------------------------------

    def _render_status(
        self, status: StatusList,
    ) -> tuple[str, list[int], dict[int, str]]:
        """Convert a list of status entries to display text.

        Each entry is normalised to a :class:`StatusEntry`,
        then rendered as a separator line, a heading, and
        remaining fields indented by four spaces.

        Returns ``(text, heading_lines, line_tooltips)`` where
        *heading_lines* is a list of 1-based line numbers
        corresponding to headings and *line_tooltips* maps
        1-based line numbers to tooltip strings.
        """
        parts: list[str] = []
        heading_lines: list[int] = []
        line_tooltips: dict[int, str] = {}

        for raw_entry in status:
            try:
                entry = self._normalize_entry(raw_entry)
            except Exception as problem:  # pylint: disable=broad-except
                LOGGER.exception('Unable to normalize raw_entry=%s',
                                 raw_entry)
                entry = StatusEntry(heading='error in raw_entry', rows=[
                    StatusField(text=f'{problem=}'),
                    StatusField(text=f'{raw_entry=}')])
            parts.append(_SEPARATOR)

            if not entry.heading:
                continue

            parts.append(entry.heading)
            heading_line = len(parts)  # 1-based
            heading_lines.append(heading_line)
            if entry.heading_tooltip:
                line_tooltips[heading_line] = (
                    entry.heading_tooltip
                )

            for field in entry.rows:
                parts.append(f"    {field.text}")
                if field.tooltip:
                    line_tooltips[len(parts)] = field.tooltip
        try:
            parts = '\n'.join(parts)
        except Exception as problem:
            LOGGER.exception('Unable to join parts')
            parts = (f'Unable to join parts due to error:\n{problem=}'
                     f'\n\n{parts=}\n\n')

        return parts, heading_lines, line_tooltips

    def _apply_status(self, status: StatusList):
        """Render status and update the text area if changed.

        If hover-pause is active the update is stashed in
        ``_pending_status`` (replacing any previous pending
        update) and the display is left untouched.

        Skips the redraw when the rendered text is identical
        to the previous update, avoiding unnecessary flicker.
        """
        if self._hover_paused:
            self._pending_status = status
            return

        rendered, heading_lines, line_tooltips = (
            self._render_status(status)
        )
        if rendered == self._last_rendered:
            self._update_timestamp()
            return
        self._last_rendered = rendered
        self._line_tooltips = line_tooltips
        self._set_text(rendered)
        self._apply_heading_tags(heading_lines)
        self._apply_highlight_tags(rendered)
        self._update_timestamp()

    def _show_error(self, message: str):
        """Display an error message in the text area."""
        error_text = f"{_SEPARATOR}\nERROR\n    {message}"
        self._line_tooltips = {}
        self._set_text(error_text)
        self._apply_highlight_tags(error_text)
        self._update_timestamp()

    def _set_text(self, content: str):
        """Replace the text area contents and scroll to top."""
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("end", content)
        self._text.see("1.0")
        # Leave state="normal" — caller is responsible for
        # switching back to disabled after tagging is done.

    def _apply_heading_tags(self, heading_lines: list[int]):
        """Apply the bold heading tag to each heading line."""
        for line_no in heading_lines:
            start = f"{line_no}.0"
            end = f"{line_no}.end"
            self._text.tag_add(_HEADING_TAG, start, end)

    def _apply_highlight_tags(self, content: str):
        """Apply regex highlight tags to the text area.

        Iterates over every line and, for each highlight rule,
        uses ``re.finditer`` to locate all non-overlapping
        matches.  If the pattern contains a capturing group,
        only group 1 is tagged; otherwise the full match is
        tagged.  This lets a pattern use surrounding text as
        context without highlighting it.

        Tags are applied so that the first rule in the
        highlights list has the highest display priority
        (configured once in ``_configure_tags``).

        After all tags are applied the text area is set back
        to the disabled (read-only) state.
        """
        lines = content.split("\n")
        for line_idx, line in enumerate(lines):
            tk_line = line_idx + 1  # tk.Text is 1-based
            for pattern, tag_name, _config, _cb in (
                self._highlights
            ):
                for match in pattern.finditer(line):
                    group = 1 if match.lastindex else 0
                    start = f"{tk_line}.{match.start(group)}"
                    end = f"{tk_line}.{match.end(group)}"
                    self._text.tag_add(tag_name, start, end)
        self._text.config(state="disabled")

    def _update_timestamp(self):
        """Update the footer label with the current time."""
        now = datetime.now().strftime("%H:%M:%S")
        self._timestamp_label.config(
            text=f"Last updated: {now}",
        )

    # ----------------------------------------------------------
    # Background polling
    # ----------------------------------------------------------

    def _poll_loop(self):
        """Background thread loop that polls the updater.

        Sleeps for the configured interval between polls.
        Respects the pause event and the stop event.  Uses
        ``Event.wait`` so that the thread wakes immediately
        when the window closes.
        """
        while not self._stop_event.is_set():
            self._pause_event.wait()
            if self._stop_event.is_set():
                break

            status = self._fetch_status()
            if status is not None and not self._stop_event.is_set():
                self.after_idle(self._apply_status, status)

            interval = self._safe_interval()
            self._stop_event.wait(timeout=interval)

    def _safe_interval(self) -> float:
        """Read the interval variable with a sensible fallback.

        Returns the user-entered value, clamped to a minimum
        of 0.01 seconds.  Falls back to 0.2 if the value
        cannot be read.
        """
        try:
            return max(self._interval_var.get(), 0.01)
        except (tk.TclError, ValueError):
            return 0.2

    # ----------------------------------------------------------
    # Shutdown
    # ----------------------------------------------------------

    def _on_close(self):
        """Handle window close: stop the thread and destroy."""
        self._stop_event.set()
        self._pause_event.set()  # unblock if paused
        self._hide_tooltip()
        self.destroy()
