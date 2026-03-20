"""Lightweight status display widget for tkinter.

Provides :class:`SimpleStatusDisplay`, a ``tk.Toplevel`` window
that periodically calls a user-supplied callable and displays
the resulting status tuples in a scrollable text area.  Heading
lines (the first element of each tuple) are automatically
rendered in bold.

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
from typing import Any, Callable, Dict, List, Tuple

from ox_ui.tkdantic.builder import add_tooltip

LOGGER = logging.getLogger(__name__)

# Type alias for the updater return value.
StatusList = List[Tuple[str, ...]]

# Type alias for a single highlight rule.
HighlightRule = Tuple[str, Dict[str, Any]]

# Separator drawn between status items.
_SEPARATOR = "-" * 40

# Tag name for auto-bold headings.
_HEADING_TAG = "_heading"


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
        string tuples.  The first element of each tuple is
        treated as a heading; the rest are indented detail
        lines.
    update_interval :
        Seconds between automatic refreshes (default 0.2).
    highlights :
        Optional list of ``(pattern, tag_config)`` tuples.
        *pattern* is a regex string matched with
        ``re.finditer`` (substring matches are supported).
        If the pattern contains a capturing group, only
        group 1 is highlighted; the rest of the pattern
        serves as context.  *tag_config* is a dict of
        ``tk.Text.tag_configure`` keyword arguments (e.g.
        ``foreground``, ``background``, ``font``,
        ``underline``).  Earlier entries in the list take
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

        self._init_threading()
        self._build_controls(update_interval)
        self._build_status_area()
        self._configure_tags()
        self._build_footer()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_polling()

    # ----------------------------------------------------------
    # Highlight compilation and tag setup
    # ----------------------------------------------------------

    @staticmethod
    def _compile_highlights(
        rules: List[HighlightRule],
    ) -> list[tuple[re.Pattern, str, dict[str, Any]]]:
        """Compile regex patterns and assign tag names.

        Returns a list of ``(compiled_pattern, tag_name,
        tag_config)`` triples.
        """
        compiled: list[
            tuple[re.Pattern, str, dict[str, Any]]
        ] = []
        for idx, (pattern, config) in enumerate(rules):
            tag_name = f"_hl_{idx}"
            compiled.append((re.compile(pattern), tag_name, config))
        return compiled

    def _configure_tags(self):
        """Configure the heading and highlight tags.

        The heading tag gets the lowest priority and each
        highlight tag is raised so that the first entry in
        the *highlights* list has the highest priority.
        """
        # Heading tag: bold variant of the base font.
        self._text.tag_configure(
            _HEADING_TAG, font=("Consolas", 10, "bold"),
        )

        for _pattern, tag_name, config in self._highlights:
            self._text.tag_configure(tag_name, **config)

        # Set priority: heading lowest, then highlights in
        # reverse order so index 0 ends up highest.
        self._text.tag_lower(_HEADING_TAG)
        for _pattern, tag_name, _config in reversed(
            self._highlights
        ):
            self._text.tag_raise(tag_name)

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

    def _build_footer(self):
        """Create the footer with the last-updated timestamp."""
        footer = ttk.Frame(self, padding=(8, 0, 8, 6))
        footer.pack(fill="x")

        self._timestamp_label = ttk.Label(
            footer, text="Last updated: --",
        )
        self._timestamp_label.pack(side="left")

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
        """Force a single synchronous refresh of the display."""
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
            return self._updater()
        except Exception as exc:
            LOGGER.exception(
                "Updater raised an exception: %s", exc,
            )
            self._show_error(str(exc))
            return None

    # ----------------------------------------------------------
    # Rendering
    # ----------------------------------------------------------

    def _render_status(
        self, status: StatusList,
    ) -> tuple[str, list[int]]:
        """Convert a list of status tuples to display text.

        Each tuple is rendered as a separator line, a heading
        (the first element), and remaining elements indented
        by four spaces.

        Returns ``(text, heading_lines)`` where
        *heading_lines* is a list of 1-based line numbers
        corresponding to headings.
        """
        parts: list[str] = []
        heading_lines: list[int] = []
        for entry in status:
            parts.append(_SEPARATOR)
            if not entry:
                continue
            parts.append(entry[0])
            heading_lines.append(len(parts))  # 1-based
            for detail in entry[1:]:
                parts.append(f"    {detail}")
        return "\n".join(parts), heading_lines

    def _apply_status(self, status: StatusList):
        """Render status and update the text area if changed.

        Skips the redraw when the rendered text is identical
        to the previous update, avoiding unnecessary flicker.
        """
        rendered, heading_lines = self._render_status(status)
        if rendered == self._last_rendered:
            self._update_timestamp()
            return
        self._last_rendered = rendered
        self._set_text(rendered)
        self._apply_heading_tags(heading_lines)
        self._apply_highlight_tags(rendered)
        self._update_timestamp()

    def _show_error(self, message: str):
        """Display an error message in the text area."""
        error_text = f"{_SEPARATOR}\nERROR\n    {message}"
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
            for pattern, tag_name, _config in self._highlights:
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
        self.destroy()
