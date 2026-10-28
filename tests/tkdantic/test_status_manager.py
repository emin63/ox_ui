"""Tests for :mod:`ox_ui.tkdantic.status_manager`."""

from ox_ui.tkdantic.status_manager import Status


class TestStatus:
    """Tests for :class:`Status`."""

    def test_empty(self):
        """A new Status has no text."""
        status = Status()
        assert status.get_status_text() == ''
        assert status.get_raw_status() == ''

    def test_set_replaces(self):
        """set_status replaces the raw text."""
        status = Status()
        status.set_status('one')
        status.set_status('two')
        assert status.get_raw_status() == 'two'

    def test_append(self):
        """append_status adds a line, without a leading newline."""
        status = Status()
        status.append_status('first')
        status.append_status('second')
        assert status.get_raw_status() == 'first\nsecond'

    def test_headers_come_first(self):
        """Headers are shown above the raw text as they were added."""
        status = Status()
        status.set_status('working')
        status.add_status_header('file', 'file: a.txt')
        status.add_status_header('user', 'user: bob')
        assert status.get_status_text() == (
            'file: a.txt\nuser: bob\nworking')
        assert status.get_raw_status() == 'working'

    def test_header_replace_and_remove(self):
        """Headers are replaced by name and removed silently."""
        status = Status()
        status.add_status_header('file', 'file: a.txt')
        status.add_status_header('file', 'file: b.txt')
        assert status.get_status_text() == 'file: b.txt'
        status.remove_status_header('file')
        status.remove_status_header('missing')
        assert status.get_status_text() == ''

    def test_headers_without_raw_text(self):
        """With no raw text, only headers are shown."""
        status = Status()
        status.add_status_header('a', 'A')
        assert status.get_status_text() == 'A'

    def test_clear(self):
        """clear() removes both headers and raw text."""
        status = Status()
        status.set_status('x')
        status.add_status_header('a', 'A')
        status.clear()
        assert status.get_status_text() == ''
