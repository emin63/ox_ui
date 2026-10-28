"""Small helpers shared by the tkdantic GUI tests."""


class ConstantFn:
    """Callable that records its calls and returns a fixed value.

    Used to replace ``filedialog`` and ``messagebox`` functions so
    no dialog ever blocks a test.
    """

    def __init__(self, value=None):
        """Remember the value to return."""
        self.value = value
        self.calls = []

    def __call__(self, *args, **kwargs):
        """Record the call and return the fixed value."""
        self.calls.append((args, kwargs))
        return self.value


def find_widgets(widget, cls, text=None):
    """Return descendants of *widget* that are instances of *cls*.

    If *text* is given, keep only widgets whose ``text`` option
    equals it.  The search is depth first, oldest child first.
    """
    found = []
    for child in widget.winfo_children():
        if isinstance(child, cls):
            if text is None or str(child.cget('text')) == text:
                found.append(child)
        found.extend(find_widgets(child, cls, text))
    return found


def text_content(text_widget):
    """Return the contents of a Text widget without the final newline."""
    return text_widget.get('1.0', 'end-1c')


def pending_after_scripts(root):
    """Return the Tcl scripts of all pending ``after`` callbacks."""
    scripts = []
    for after_id in root.tk.splitlist(root.tk.call('after', 'info')):
        info = root.tk.splitlist(root.tk.call('after', 'info', after_id))
        scripts.append(str(info[0]))
    return scripts


def is_packed(widget):
    """Return True if *widget* is managed by the pack geometry manager."""
    return widget.winfo_manager() == 'pack'
