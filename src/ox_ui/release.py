"""Tag a release and push it to one or all git remotes.

``ox_ui release REMOTE`` (or ``ox_ui release all``), run from the
main branch of a repository, does this and stops at the first
problem:

1. Reads the package version (see :func:`find_version`) and
   requires the form ``X.Y.Z``.
2. Requires the ``main`` or ``master`` branch with no uncommitted
   changes to tracked files.
3. Checks each chosen remote: its branch must be the same as the
   local one or behind it (a fast-forward), and any tag
   ``v<version>`` it already has must be this commit.
4. Shows the plan and asks for confirmation (``--yes`` skips it).
5. Creates the annotated tag ``v<version>`` if it does not exist,
   then pushes the branch and the tag (and nothing else) to each
   chosen remote, without ever forcing, and reads the tag back.

Run it again after a failure: finished steps are skipped.  A tag
never moves, so a tag of the same name on another commit, here
or on a chosen remote, is an error.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from typing import Optional

import click

RELEASE_BRANCHES = ('main', 'master')
VERSION_RE = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')
ASSIGN_RE = re.compile(
    r'^(?:__version__|VERSION)\s*(?::\s*str\s*)?=\s*'
    r'[\'"]([^\'"]+)[\'"]', re.MULTILINE)


class ReleaseError(click.ClickException):
    """A check failed; nothing further is done."""


def git(*args: str, cwd: pathlib.Path, check: bool = True) -> str:
    """Run git with *args* in *cwd* and return its stripped stdout.

    Raises ReleaseError if git fails and *check* is true.
    """
    proc = subprocess.run(
        ['git', *args], cwd=cwd, capture_output=True, text=True,
        check=False)
    if check and proc.returncode != 0:
        raise ReleaseError(
            f'git {" ".join(args)} failed:\n{proc.stderr.strip()}')
    return proc.stdout.strip()


def git_ok(*args: str, cwd: pathlib.Path) -> bool:
    """Run git with *args* in *cwd*; return True if it succeeded."""
    return subprocess.run(
        ['git', *args], cwd=cwd, capture_output=True,
        check=False).returncode == 0


# -- version ----------------------------------------------------------

def _version_in_file(path: pathlib.Path,
                     name: Optional[str] = None) -> str:
    """Return the version assigned in the Python file *path*.

    Looks for ``__version__ = '...'`` or ``VERSION = '...'`` (or
    only *name* if given).
    """
    if not path.is_file():
        raise ReleaseError(f'Version file {path} does not exist.')
    text = path.read_text(encoding='utf-8')
    if name is None:
        match = ASSIGN_RE.search(text)
    else:
        match = re.search(
            rf'^{re.escape(name)}\s*(?::\s*str\s*)?=\s*'
            r'[\'"]([^\'"]+)[\'"]', text, re.MULTILINE)
    if not match:
        raise ReleaseError(f'No version assignment found in {path}.')
    return match.group(1)


def _setuptools_attr_file(
        root: pathlib.Path, attr: str) -> tuple[pathlib.Path, str]:
    """Return (file, variable) for a setuptools ``attr`` version."""
    module, _, name = attr.rpartition('.')
    parts = module.split('.')
    for base in (root / 'src', root):
        for candidate in (base.joinpath(*parts).with_suffix('.py'),
                          base.joinpath(*parts, '__init__.py')):
            if candidate.is_file():
                return candidate, name
    raise ReleaseError(f'Cannot find module {module} for {attr}.')


def find_version(root: pathlib.Path,
                 version_file: Optional[pathlib.Path] = None) -> str:
    """Return the package version of the project in *root*.

    With *version_file*, the version is the ``__version__`` or
    ``VERSION`` assigned there.  Otherwise ``pyproject.toml`` is
    read: a static ``[project] version``, or for a dynamic version
    the file named by ``[tool.hatch.version] path`` or by
    setuptools' ``[tool.setuptools.dynamic] version`` (``attr`` or
    ``file``).
    """
    if version_file is not None:
        return _version_in_file(root / version_file)
    pyproject = root / 'pyproject.toml'
    if not pyproject.is_file():
        raise ReleaseError(
            'No pyproject.toml here; use --version-file.')
    with pyproject.open('rb') as fd:
        data = tomllib.load(fd)
    project = data.get('project', {})
    if 'version' in project:
        return str(project['version'])
    tool = data.get('tool', {})
    hatch_path = tool.get('hatch', {}).get('version', {}).get('path')
    if hatch_path:
        return _version_in_file(root / hatch_path)
    dynamic = tool.get('setuptools', {}).get(
        'dynamic', {}).get('version', {})
    if 'attr' in dynamic:
        return _version_in_file(
            *_setuptools_attr_file(root, dynamic['attr']))
    if 'file' in dynamic:
        files = dynamic['file']
        name = files[0] if isinstance(files, list) else files
        return (root / name).read_text(encoding='utf-8').strip()
    raise ReleaseError(
        'Cannot tell where the version lives from pyproject.toml; '
        'use --version-file.')


def _version_key(version: str) -> tuple[int, ...]:
    """Return *version* (``X.Y.Z``) as a tuple for comparison."""
    return tuple(int(part) for part in version.split('.'))


# -- checks -------------------------------------------------------------

@dataclass
class RemotePlan:
    """What to push to one remote."""

    remote: str
    old_head: str        # the remote's branch commit ('' if none)
    push_branch: bool
    push_tag: bool


def _remote_refs(root: pathlib.Path, remote: str,
                 *refs: str) -> dict[str, str]:
    """Return {ref name: object id} of *refs* on *remote*."""
    out = git('ls-remote', remote, *refs, cwd=root)
    result = {}
    for line in out.splitlines():
        oid, _, name = line.partition('\t')
        result[name] = oid
    return result


def _check_remote(root: pathlib.Path, remote: str, branch: str,
                  head: str, tag: str,
                  tag_obj: Optional[str]) -> RemotePlan:
    """Check *remote* and return what to push to it."""
    tag_ref = f'refs/tags/{tag}'
    refs = _remote_refs(root, remote, f'refs/heads/{branch}',
                        tag_ref, tag_ref + '^{}')
    old = refs.get(f'refs/heads/{branch}', '')
    if old and old != head:
        # Unknown here, or not an ancestor: the remote is ahead or
        # has diverged, and pushing would need a force.
        if not (git_ok('cat-file', '-e', old + '^{commit}', cwd=root)
                and git_ok('merge-base', '--is-ancestor', old, head,
                           cwd=root)):
            raise ReleaseError(
                f'{remote}/{branch} ({old[:12]}) has commits that '
                f'are not here: pull them first (git pull {remote} '
                f'{branch}).')
    there = refs.get(tag_ref)
    if there is not None:
        peeled = refs.get(tag_ref + '^{}', there)
        if peeled != head:
            raise ReleaseError(
                f'{remote} already has {tag} at {peeled[:12]}, not '
                f'at {head[:12]}; a tag never moves (bump the '
                'version?).')
        if tag_obj is None:
            raise ReleaseError(
                f'{remote} already has {tag} but this clone does '
                f'not: git fetch {remote} tag {tag}, then run again.')
        if there != tag_obj:
            raise ReleaseError(
                f'{remote} has a different {tag} object '
                f'({there[:12]}, here {tag_obj[:12]}).')
    return RemotePlan(remote, old, push_branch=old != head,
                      push_tag=there is None)


def _check_local_tag(root: pathlib.Path, tag: str,
                     head: str) -> Optional[str]:
    """Return the local tag object id, or None if there is no tag.

    Raises ReleaseError if the tag is not an annotated tag of
    *head*.
    """
    tag_obj = git('rev-parse', '-q', '--verify', f'refs/tags/{tag}',
                  cwd=root, check=False)
    if not tag_obj:
        return None
    if git('cat-file', '-t', tag_obj, cwd=root) != 'tag':
        raise ReleaseError(f'{tag} here is not an annotated tag.')
    commit = git('rev-parse', f'{tag}^{{commit}}', cwd=root)
    if commit != head:
        raise ReleaseError(
            f'{tag} already exists here at {commit[:12]}, not at '
            f'HEAD {head[:12]}; a tag never moves (bump the version?).')
    return tag_obj


def _check_above_latest(root: pathlib.Path, version: str) -> None:
    """Require *version* to be above every release tag on HEAD."""
    tags = git('tag', '-l', 'v[0-9]*', '--merged', 'HEAD', cwd=root)
    older = [t[1:] for t in tags.split()
             if VERSION_RE.match(t[1:]) and t[1:] != version]
    if older:
        latest = max(older, key=_version_key)
        if _version_key(version) <= _version_key(latest):
            raise ReleaseError(
                f'{version} is not above the latest release tag '
                f'v{latest}.')


# -- command ------------------------------------------------------------

@click.command('release')
@click.argument('remote', required=False)
@click.option('--version-file', type=click.Path(path_type=pathlib.Path),
              help='Python file assigning __version__ (or VERSION); '
              'default: found from pyproject.toml.')
@click.option('--yes', '-y', is_flag=True,
              help='Do not ask before creating and pushing.')
def release(remote: Optional[str], version_file: Optional[pathlib.Path],
            yes: bool) -> None:
    """Tag v<version> on main/master and push it to REMOTE.

    REMOTE is a git remote name, or "all" for every remote.  The
    branch is pushed too (fast-forward only), so the remote has the
    tagged commit on its main branch.  Nothing is forced, and no
    other branches or tags are pushed.
    """
    root = pathlib.Path(git('rev-parse', '--show-toplevel',
                            cwd=pathlib.Path.cwd()))
    remotes = git('remote', cwd=root).split()
    if not remote:
        click.echo('Choose where to push: ox_ui release REMOTE '
                   '(or "all" for every remote).  Remotes: '
                   + (' '.join(remotes) or '(none)'))
        sys.exit(1)
    if remote == 'all':
        targets = remotes
    elif remote in remotes:
        targets = [remote]
    else:
        raise ReleaseError(
            f'No remote {remote!r}; remotes: {" ".join(remotes)}.')
    if not targets:
        raise ReleaseError('This repository has no remotes.')

    version = find_version(root, version_file)
    if not VERSION_RE.match(version):
        raise ReleaseError(f'Version {version!r} is not X.Y.Z.')
    tag = f'v{version}'
    branch = git('rev-parse', '--abbrev-ref', 'HEAD', cwd=root)
    if branch not in RELEASE_BRANCHES:
        raise ReleaseError(
            f'Release from {" or ".join(RELEASE_BRANCHES)}, '
            f'not {branch}.')
    if git('status', '--porcelain', '--untracked-files=no', cwd=root):
        raise ReleaseError('Commit or stash your changes first.')
    head = git('rev-parse', 'HEAD', cwd=root)
    tag_obj = _check_local_tag(root, tag, head)
    if tag_obj is None:
        _check_above_latest(root, version)
    plans = [_check_remote(root, r, branch, head, tag, tag_obj)
             for r in targets]

    click.echo(f'Release {tag} at {head[:12]} on {branch}:')
    if tag_obj is None:
        click.echo(f'  create annotated tag {tag} here')
    for plan in plans:
        todo = []
        if plan.push_branch:
            todo.append(f'push {branch} ({plan.old_head[:12] or "new"}'
                        f' -> {head[:12]})')
        if plan.push_tag:
            todo.append(f'push {tag}')
        click.echo(f'  {plan.remote}: '
                   + (', '.join(todo) or 'up to date'))
    if tag_obj is not None and not any(
            p.push_branch or p.push_tag for p in plans):
        click.echo('Nothing to do.')
        return
    if not yes and not click.confirm('Go ahead?', default=False):
        raise click.Abort()

    if tag_obj is None:
        git('tag', '-a', tag, '-m', f'{root.name} {tag}', cwd=root)
        click.echo(f'Created {tag}.')
    for plan in plans:
        refspecs = []
        if plan.push_branch:
            refspecs.append(f'HEAD:refs/heads/{branch}')
        if plan.push_tag:
            refspecs.append(f'refs/tags/{tag}')
        if not refspecs:
            continue
        click.echo(f'Pushing to {plan.remote}: {" ".join(refspecs)}')
        git('push', '--atomic', plan.remote, *refspecs, cwd=root)
        got = _remote_refs(root, plan.remote,
                           f'refs/tags/{tag}^{{}}').get(
                               f'refs/tags/{tag}^{{}}')
        if got != head:
            raise ReleaseError(
                f'{plan.remote} reports {tag} at {got!r}, expected '
                f'{head}.')
        click.echo(f'  {plan.remote}: {tag} -> {head[:12]}')
