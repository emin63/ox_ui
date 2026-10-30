"""Tests for ``ox_ui release``, against throwaway repositories.

Each test makes a working clone with two local bare repositories
as remotes, so nothing leaves the temporary directory.
"""

import pathlib
import subprocess

import pytest
from click.testing import CliRunner

from ox_ui.cli import main
from ox_ui.release import ReleaseError, find_version


def run(*args, cwd):
    """Run a command in *cwd* and return its stripped stdout."""
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True,
        text=True).stdout.strip()


@pytest.fixture(autouse=True)
def git_env(monkeypatch, tmp_path):
    """Isolate git from the user's and system's configuration."""
    empty = tmp_path / 'gitconfig'
    empty.write_text('')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(empty))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    for who in ('AUTHOR', 'COMMITTER'):
        monkeypatch.setenv(f'GIT_{who}_NAME', 'Tester')
        monkeypatch.setenv(f'GIT_{who}_EMAIL', 'tester@example.com')


PYPROJECT = '''[project]
name = "demo"
version = "{version}"
'''


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone on main with remotes "origin" and "mirror"."""
    for name in ('origin', 'mirror'):
        run('git', 'init', '-q', '--bare', '-b', 'main',
            str(tmp_path / f'{name}.git'), cwd=tmp_path)
    work = tmp_path / 'work'
    run('git', 'init', '-q', '-b', 'main', str(work), cwd=tmp_path)
    (work / 'pyproject.toml').write_text(PYPROJECT.format(version='1.2.3'))
    run('git', 'add', '-A', cwd=work)
    run('git', 'commit', '-q', '-m', 'init', cwd=work)
    for name in ('origin', 'mirror'):
        run('git', 'remote', 'add', name, str(tmp_path / f'{name}.git'),
            cwd=work)
    run('git', 'push', '-q', 'origin', 'main', cwd=work)
    monkeypatch.chdir(work)
    return work


def release(*args, input=None):
    """Run ``ox_ui release ARGS`` and return the click result."""
    return CliRunner().invoke(main, ['release', *args], input=input)


def remote_tag(repo, remote, tag='v1.2.3'):
    """Commit of *tag* on *remote* ('' if none)."""
    out = run('git', 'ls-remote', remote, f'refs/tags/{tag}^{{}}',
              cwd=repo)
    return out.split('\t')[0]


def remote_main(repo, remote):
    """Commit of main on *remote* ('' if none)."""
    out = run('git', 'ls-remote', remote, 'refs/heads/main', cwd=repo)
    return out.split('\t')[0]


def head(repo):
    """The current commit."""
    return run('git', 'rev-parse', 'HEAD', cwd=repo)


def commit(repo, version, message='change'):
    """Commit a pyproject.toml with *version*."""
    (repo / 'pyproject.toml').write_text(PYPROJECT.format(version=version))
    run('git', 'commit', '-q', '--allow-empty', '-am', message, cwd=repo)


def test_no_remote_lists_remotes(repo):
    """Without REMOTE nothing happens and the remotes are shown."""
    result = release()
    assert result.exit_code == 1
    assert 'mirror origin' in result.output
    assert run('git', 'tag', cwd=repo) == ''


def test_release_one_remote(repo):
    """Tag here, push branch and tag to that remote only."""
    commit(repo, '1.2.3', 'second')
    result = release('origin', '--yes')
    assert result.exit_code == 0, result.output
    assert 'origin: push main' in result.output
    assert run('git', 'cat-file', '-t', 'v1.2.3', cwd=repo) == 'tag'
    assert remote_tag(repo, 'origin') == head(repo)
    assert remote_main(repo, 'origin') == head(repo)
    assert remote_tag(repo, 'mirror') == ''
    assert remote_main(repo, 'mirror') == ''


def test_release_all_then_nothing_to_do(repo):
    """"all" covers every remote; a second run does nothing."""
    result = release('all', '--yes')
    assert result.exit_code == 0, result.output
    for name in ('origin', 'mirror'):
        assert remote_tag(repo, name) == head(repo)
        assert remote_main(repo, name) == head(repo)
    again = release('all', '--yes')
    assert again.exit_code == 0, again.output
    assert 'Nothing to do.' in again.output


def test_later_remote_after_first(repo):
    """An existing local tag is pushed to a remote that lacks it."""
    assert release('origin', '--yes').exit_code == 0
    result = release('mirror', '--yes')
    assert result.exit_code == 0, result.output
    assert 'create annotated tag' not in result.output
    assert remote_tag(repo, 'mirror') == head(repo)


def test_confirmation_declined(repo):
    """Answering no creates and pushes nothing."""
    result = release('origin', input='n\n')
    assert result.exit_code != 0
    assert run('git', 'tag', cwd=repo) == ''
    assert remote_tag(repo, 'origin') == ''


def test_unknown_remote(repo):
    """A remote name that does not exist is an error."""
    result = release('nope', '--yes')
    assert result.exit_code != 0
    assert "No remote 'nope'" in result.output


def test_not_on_main(repo):
    """Releases come from main or master only."""
    run('git', 'switch', '-q', '-c', 'feature', cwd=repo)
    result = release('origin', '--yes')
    assert result.exit_code != 0
    assert 'not feature' in result.output


def test_master_is_allowed(repo):
    """A master branch works like main."""
    run('git', 'branch', '-q', '-m', 'master', cwd=repo)
    result = release('mirror', '--yes')
    assert result.exit_code == 0, result.output
    out = run('git', 'ls-remote', 'mirror', 'refs/heads/master', cwd=repo)
    assert out.split('\t')[0] == head(repo)


def test_dirty_tree(repo):
    """Uncommitted changes to tracked files stop the release."""
    (repo / 'pyproject.toml').write_text(PYPROJECT.format(version='9.9.9'))
    result = release('origin', '--yes')
    assert result.exit_code != 0
    assert 'Commit or stash' in result.output


def test_bad_version(repo):
    """The version must be X.Y.Z."""
    commit(repo, '1.2.3rc1')
    result = release('origin', '--yes')
    assert result.exit_code != 0
    assert 'is not X.Y.Z' in result.output


def test_remote_ahead(repo, tmp_path):
    """A remote with commits not here is not overwritten."""
    other = tmp_path / 'other'
    run('git', 'clone', '-q', str(tmp_path / 'origin.git'), str(other),
        cwd=tmp_path)
    (other / 'extra.txt').write_text('x')
    run('git', 'add', 'extra.txt', cwd=other)
    run('git', 'commit', '-q', '-m', 'extra', cwd=other)
    run('git', 'push', '-q', 'origin', 'main', cwd=other)
    before = remote_main(repo, 'origin')
    result = release('origin', '--yes')
    assert result.exit_code != 0
    assert 'pull them first' in result.output
    assert remote_main(repo, 'origin') == before
    assert run('git', 'tag', cwd=repo) == ''


def test_tag_on_other_commit(repo):
    """An existing tag of the same name never moves."""
    assert release('origin', '--yes').exit_code == 0
    commit(repo, '1.2.3', 'more')
    result = release('origin', '--yes')
    assert result.exit_code != 0
    assert 'a tag never moves' in result.output


def test_remote_has_tag_this_clone_lacks(repo):
    """A tag made elsewhere must be fetched, not re-created."""
    assert release('origin', '--yes').exit_code == 0
    run('git', 'tag', '-d', 'v1.2.3', cwd=repo)
    result = release('origin', '--yes')
    assert result.exit_code != 0
    assert 'git fetch origin tag v1.2.3' in result.output


def test_version_not_above_latest(repo):
    """A new version must be above the latest release tag."""
    commit(repo, '2.0.0')
    assert release('origin', '--yes').exit_code == 0
    commit(repo, '1.9.0')
    result = release('origin', '--yes')
    assert result.exit_code != 0
    assert 'not above the latest release tag v2.0.0' in result.output


# -- find_version -------------------------------------------------------

def test_find_version_static(tmp_path):
    """A static [project] version."""
    (tmp_path / 'pyproject.toml').write_text(PYPROJECT.format(
        version='0.1.0'))
    assert find_version(tmp_path) == '0.1.0'


def test_find_version_hatch_path(tmp_path):
    """A dynamic version read by hatch from a file."""
    (tmp_path / 'pyproject.toml').write_text(
        '[project]\nname = "demo"\ndynamic = ["version"]\n'
        '[tool.hatch.version]\npath = "src/demo/__init__.py"\n')
    pkg = tmp_path / 'src' / 'demo'
    pkg.mkdir(parents=True)
    (pkg / '__init__.py').write_text('"""Doc."""\n\n__version__ = "0.2.0"\n')
    assert find_version(tmp_path) == '0.2.0'


def test_find_version_setuptools_attr(tmp_path):
    """A dynamic version from setuptools' attr, named VERSION."""
    (tmp_path / 'pyproject.toml').write_text(
        '[project]\nname = "demo"\ndynamic = ["version"]\n'
        '[tool.setuptools.dynamic]\nversion = {attr = "demo.VERSION"}\n')
    pkg = tmp_path / 'demo'
    pkg.mkdir()
    (pkg / '__init__.py').write_text("VERSION = '0.3.0'\n")
    assert find_version(tmp_path) == '0.3.0'


def test_find_version_file_option(tmp_path):
    """--version-file works without a pyproject.toml."""
    (tmp_path / 'version.py').write_text("__version__: str = '0.4.0'\n")
    assert find_version(tmp_path, pathlib.Path('version.py')) == '0.4.0'
    with pytest.raises(ReleaseError):
        find_version(tmp_path)


def test_find_version_of_this_package():
    """This package's own version is found and is X.Y.Z."""
    root = pathlib.Path(__file__).resolve().parents[1]
    import ox_ui
    assert find_version(root) == ox_ui.__version__
