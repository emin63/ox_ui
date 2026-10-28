"""Thread-safe status container for :class:`RunnableMachineHelper`.

This module defines :class:`Status`, a small thread-safe object
that owns the human-readable status text displayed to the user
while a state machine is running.

A :class:`Status` instance can be shared: a
:class:`RunnableMachineHelper` holds one as its public
``.status`` attribute, and can pass the same instance to helper
functions or other objects so they can update the displayed
status without being given access to the full helper.

The status is composed of two parts:

* **Headers**: named, ordered entries intended for persistent
  context (e.g. ``"file: foo.txt"``).  Added and removed by key.
* **Raw text**: a single free-form message describing what the
  worker is doing right now.

:meth:`get_status_text` returns the headers followed by the raw
text, joined by newlines, suitable for direct display.
"""

import threading


class Status:
    """Thread-safe container for status text and named headers.

    All public methods acquire an internal lock, so instances
    are safe to share across threads.  The lock is private; do
    not rely on being able to acquire it externally.
    """

    def __init__(self) -> None:
        """Initialise with empty status and no headers."""
        self._lock = threading.Lock()
        self._status_text: str = ''
        self._status_headers: dict[str, str] = {}

    def get_status_text(self) -> str:
        """Return headers (if any) followed by the raw status.

        The returned string is for display only; headers and
        the raw status text are stored separately.
        """
        with self._lock:
            parts = list(self._status_headers.values())
            if self._status_text:
                parts.append(self._status_text)
            return '\n'.join(parts)

    def get_raw_status(self) -> str:
        """Return just the raw status text, without headers."""
        with self._lock:
            return self._status_text

    def set_status(self, text: str) -> None:
        """Replace the raw status text.

        This replaces only the raw portion; status headers are
        unaffected.

        :param text: human-readable status message.
        """
        with self._lock:
            self._status_text = text

    def append_status(self, text: str) -> None:
        """Append to the raw status text.

        Useful when a caller wants to add detail without
        overwriting what is already there.

        :param text: text to append (a newline is prepended
            automatically if the raw status is non-empty).
        """
        with self._lock:
            if self._status_text:
                self._status_text += '\n' + text
            else:
                self._status_text = text

    def add_status_header(self, name: str, text: str) -> None:
        """Add or replace a named status header.

        Headers are displayed above the raw status text by
        :meth:`get_status_text`.

        :param name: unique key for this header.
        :param text: header text to display.
        """
        with self._lock:
            self._status_headers[name] = text

    def remove_status_header(self, name: str) -> None:
        """Remove a status header by name.

        Silently ignores unknown names.

        :param name: key passed to :meth:`add_status_header`.
        """
        with self._lock:
            self._status_headers.pop(name, None)

    def clear(self) -> None:
        """Clear all status text and headers.

        Intended for use when resetting run state before a
        new transition.
        """
        with self._lock:
            self._status_text = ''
            self._status_headers.clear()
