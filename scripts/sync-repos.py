"""sync-repos - recursively find every git repo under one or more directories,
fetch every remote in parallel, fast-forward the local branches that can move
safely, delete the local branches that were already merged, and report exactly
what arrived.

With no DIR arguments, the directories come from CONFIG
(~/.config/sync-repos/config.yaml):

  directories:
    - ~/Codes

Entries may use ~ and $VARS. An entry that is not a directory on this machine
is skipped with a warning, so one config can be shared across machines.

MODIFIES LOCAL BRANCHES. Reversible, but not a pure read like a plain fetch:

  * A branch is only ever touched when it is *strictly behind* its upstream -
    zero commits ahead. No local commit is discarded, no merge commit is
    created, no working-tree file is rewritten beyond what the incoming commits
    change. Diverged, ahead-only, and upstream-less branches are reported and
    left alone.
  * The checked-out branch moves via `git merge --ff-only`, and only when the
    tracked working tree is clean and no rebase/merge/cherry-pick/bisect is in
    progress. Otherwise it is reported as "not fast-forwarded" and skipped.
  * Other local branches move via `git fetch . <upstream>:<branch>`, which git
    itself refuses if the move is not a fast-forward, or if the branch is
    checked out in another worktree.
  * `--prune` removes stale remote-tracking refs (e.g. origin/foo after foo was
    deleted upstream) - never local branches or commits.
  * A local branch is deleted with `git branch -D` when its content is already
    in a remote's default branch (whatever refs/remotes/<remote>/HEAD points
    at), whether it landed by merge, fast-forward, rebase, or squash. Only
    branches that track a remote branch qualify, never one that tracks a
    default branch itself (local main). Squash and rebase merges are
    recognized by patch-id, or by a merged GitHub pull request (via `gh`)
    whose head contains the branch, and any doubt keeps the branch. Remote
    branches are never deleted.
  * While its remote branch still exists, a branch is kept when it has no
    commits of its own (its tip sits on the default branch's first-parent
    history, so it is fresh work in progress) or when it is checked out in any
    worktree.
  * Once its remote branch is gone, a merged branch that is checked out here
    is switched away from: `git switch` to the local branch tracking that
    default branch, carrying uncommitted changes along. git refuses if they
    overlap with the incoming files, and the branch is then kept. A gone
    branch that is not proven merged is flagged and kept.
  * `git worktree prune` drops records of worktrees whose directory no longer
    exists. Worktree directories themselves are never removed.
  * `-n` / `--fetch-only` disables all fast-forwarding, switching, deleting,
    and worktree pruning: fetch and report only.

Every fast-forward lands in the branch's reflog, so a single move is undone
with `git reset --hard <branch>@{1}`. A deleted branch takes its reflog with
it, so each deletion is appended to JOURNAL
(${XDG_STATE_HOME:-~/.local/state}/sync-repos/deleted-branches.tsv) as
timestamp, repo, branch, full sha, and the target it was found in; restore one
with `git branch <branch> <sha>` before gc prunes its commits.

Usage: sync-repos [-j JOBS] [-n] [DIR ...]
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

CONFIG = os.path.expanduser("~/.config/sync-repos/config.yaml")
JOURNAL = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "sync-repos", "deleted-branches.tsv",
)

# A single unreachable remote must not stall the whole run.
FETCH_TIMEOUT = 300
GH_TIMEOUT = 60

GITHUB_URL = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])"
    r"([^/]+/[^/]+?)(?:\.git)?/?$"
)

# Files in the git dir that mean "an operation is half-finished here".
IN_PROGRESS = (
    ("rebase-merge", "rebase in progress"),
    ("rebase-apply", "rebase or am in progress"),
    ("MERGE_HEAD", "merge in progress"),
    ("CHERRY_PICK_HEAD", "cherry-pick in progress"),
    ("REVERT_HEAD", "revert in progress"),
    ("BISECT_LOG", "bisect in progress"),
)


class Action(enum.Enum):
    UP_TO_DATE = "up to date"
    FAST_FORWARDED = "fast-forwarded"
    BEHIND = "behind"  # behind upstream, fast-forward not attempted (-n)
    BLOCKED = "blocked"  # behind upstream, but could not move
    AHEAD = "ahead"
    DIVERGED = "diverged"
    UPSTREAM_GONE = "upstream gone"
    NO_UPSTREAM = "no upstream"
    DETACHED = "detached HEAD"
    UNBORN = "no commits yet"
    DELETED = "deleted"  # already merged into a remote default branch
    MERGED_KEPT = "merged, kept"
    UNMERGED = "upstream gone, not merged"


# Actions that mean "you probably want to look at this repo".
FLAGGED = (Action.BLOCKED, Action.DIVERGED, Action.MERGED_KEPT, Action.UNMERGED)
# Non-current branches are only listed when they have something to say.
NOTABLE = (Action.FAST_FORWARDED, Action.BLOCKED, Action.BEHIND, Action.DIVERGED,
           Action.DELETED, Action.MERGED_KEPT, Action.UNMERGED)


@dataclass
class Branch:
    name: str
    action: Action
    old: str = ""
    new: str = ""
    ahead: int = 0
    behind: int = 0
    note: str = ""
    current: bool = False
    sha: str = ""  # full sha of the tip, after any fast-forward this run made
    target: str = ""  # DELETED only: the default branch it was found in
    pr: int = 0
    switched_to: str = ""
    reason: str = ""

    @property
    def flagged(self) -> bool:
        return self.action in FLAGGED


@dataclass(frozen=True)
class Target:
    ref: str
    remote: str
    branch: str

    @property
    def short(self) -> str:
        return "%s/%s" % (self.remote, self.branch)


@dataclass(frozen=True)
class Proof:
    target: Target
    pr: int = 0


@dataclass
class Repo:
    path: str
    rel: str
    ok: bool = True
    error: str = ""
    branches: List[Branch] = field(default_factory=list)
    refs_new: int = 0
    refs_pruned: int = 0
    worktrees_pruned: int = 0
    # Remote to suggest `git remote set-head` for, when merged branches may
    # exist but no remote HEAD says which branch they would be merged into.
    headless: str = ""

    @property
    def fast_forwarded(self) -> List[Branch]:
        return [b for b in self.branches if b.action is Action.FAST_FORWARDED]

    @property
    def deleted(self) -> List[Branch]:
        return [b for b in self.branches if b.action is Action.DELETED]

    @property
    def gone(self) -> int:
        return sum(1 for b in self.branches if b.action is Action.UPSTREAM_GONE)

    @property
    def flagged(self) -> bool:
        return any(b.flagged for b in self.branches)


# --- git plumbing -----------------------------------------------------------

def build_env() -> Dict[str, str]:
    """Git environment for unattended, parallel use.

    A credential or passphrase prompt from one of N concurrent workers cannot be
    answered - it would just hang the run - so make git fail fast instead.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


GIT_ENV = build_env()


def git(repo: str, *args: str, timeout: Optional[int] = None) -> Tuple[int, str]:
    """Run git in repo. Returns (returncode, stdout+stderr, stripped)."""
    try:
        p = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=GIT_ENV,
        )
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % (timeout or 0)
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


# Fixed identity and dates make the synthetic squash commit deterministic, so
# hourly runs reuse one object per branch state instead of piling up new ones.
SYNTHETIC_ENV = dict(
    GIT_ENV,
    GIT_AUTHOR_NAME="sync-repos",
    GIT_AUTHOR_EMAIL="sync-repos@localhost",
    GIT_AUTHOR_DATE="@0 +0000",
    GIT_COMMITTER_NAME="sync-repos",
    GIT_COMMITTER_EMAIL="sync-repos@localhost",
    GIT_COMMITTER_DATE="@0 +0000",
)


def git_out(repo: str, *args: str, env: Dict[str, str] = GIT_ENV) -> str:
    """Run git in repo, return stdout stripped ('' on any failure)."""
    try:
        p = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            env=env,
        )
    except OSError:
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def oneline(text: str, limit: int = 0) -> str:
    """Collapse a multi-line git error into one tidy line, optionally clipped."""
    out = " ".join(text.split())
    if limit and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def fields(line: str, n: int) -> List[str]:
    """Split a tab-separated for-each-ref line into exactly n fields.

    Trailing empty fields (an unset %(upstream), say) are lost to the strip()
    that trims the trailing newline, so pad them back rather than dropping the
    whole line.
    """
    parts = line.split("\t")
    return parts + [""] * (n - len(parts)) if len(parts) < n else parts[:n]


def remote_refs(repo: str) -> Dict[str, Tuple[str, str]]:
    """Map refs/remotes/... -> (full sha, abbreviated sha).

    refs/remotes/<remote>/HEAD is skipped: it is a symref bookkeeping entry that
    git creates on first fetch, not a branch that received commits.
    """
    out = git_out(
        repo,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)%09%(objectname:short)",
        "refs/remotes",
    )
    refs = {}
    for line in out.splitlines():
        name, sha, short = fields(line, 3)
        if name and not name.endswith("/HEAD"):
            refs[name] = (sha, short)
    return refs


def ahead_behind(repo: str, left: str, right: str) -> Tuple[int, int]:
    out = git_out(repo, "rev-list", "--left-right", "--count", "%s...%s" % (left, right))
    parts = out.split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def prune_worktrees(repo: str) -> int:
    rc, out = git(repo, "worktree", "prune", "--verbose")
    if rc != 0:
        return 0
    return sum(1 for line in out.splitlines() if line.startswith("Removing "))


def checked_out(repo: str) -> Optional[Dict[str, str]]:
    """Branches checked out in any worktree of repo, or None if unknown."""
    rc, out = git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return None
    branches = {}
    path = ""
    prefix = "branch refs/heads/"
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith(prefix):
            branches[line[len(prefix):]] = path
    return branches


def merged_targets(repo: str, after: Dict[str, Tuple[str, str]]) -> Dict[str, Target]:
    """Map each remote's default branch ref -> its Target.

    Every remote counts, not just origin: in a fork, PRs land in upstream/main
    while origin/HEAD may point at a stale copy.
    """
    out = git_out(repo, "for-each-ref", "--format=%(refname)%09%(symref)", "refs/remotes")
    targets = {}
    for line in sorted(out.splitlines()):
        name, symref = fields(line, 2)
        if not name.endswith("/HEAD") or symref not in after:
            continue
        remote = name[len("refs/remotes/"):-len("/HEAD")]
        prefix = "refs/remotes/%s/" % remote
        if symref.startswith(prefix):
            targets[symref] = Target(symref, remote, symref[len(prefix):])
    return targets


def on_first_parent(repo: str, tip: str, target: str) -> bool:
    rc, _ = git(repo, "merge-base", "--is-ancestor", tip, target)
    if rc != 0:
        return False
    chain = git_out(repo, "rev-list", "--first-parent", target, "^" + tip).splitlines()
    return not chain or git_out(repo, "rev-parse", chain[-1] + "^1") == tip


def merge_proof(repo: str, tip: str, head: str,
                targets: Dict[str, Target]) -> Tuple[Optional[Proof], str]:
    for target in targets.values():
        if contains(repo, target.ref, tip):
            return Proof(target), ""
    return github_proof(repo, tip, head, targets)


def github_proof(repo: str, tip: str, head: str,
                 targets: Dict[str, Target]) -> Tuple[Optional[Proof], str]:
    note = ""
    for target in targets.values():
        match = GITHUB_URL.match(git_out(repo, "config", "--get", "remote.%s.url" % target.remote))
        if not match:
            continue
        prs, error = pull_requests(match.group(1), head)
        note = note or error
        closed = 0
        for pr in prs:
            if pr["state"] not in ("MERGED", "CLOSED") or pr["baseRefName"] != target.branch:
                continue
            if not holds(repo, target.remote, pr, tip):
                continue
            if pr["state"] == "MERGED":
                return Proof(target, pr["number"]), ""
            closed = closed or pr["number"]
        if closed and not note:
            note = "PR #%d closed without merging" % closed
    return None, note


def pull_requests(slug: str, head: str) -> Tuple[List[dict], str]:
    try:
        p = subprocess.run(
            ["gh", "pr", "list", "--repo", slug, "--head", head, "--state", "all",
             "--json", "number,state,headRefOid,baseRefName", "--limit", "20"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            env=GIT_ENV,
        )
    except FileNotFoundError:
        return [], "gh not found"
    except subprocess.TimeoutExpired:
        return [], "gh timed out"
    except OSError as exc:
        return [], "gh: %s" % exc.strerror
    if p.returncode != 0:
        return [], "gh: " + (oneline(p.stderr, limit=60) or "exited with status %d" % p.returncode)
    try:
        prs = json.loads(p.stdout)
    except ValueError:
        return [], "gh: unreadable output"
    return sorted(prs, key=lambda pr: pr["number"], reverse=True), ""


def holds(repo: str, remote: str, pr: dict, tip: str) -> bool:
    sha = pr["headRefOid"]
    if sha == tip:
        return True
    if not git_out(repo, "rev-parse", "--verify", "--quiet", sha + "^{commit}"):
        git(repo, "fetch", "--quiet", "--no-write-fetch-head", remote,
            "refs/pull/%d/head" % pr["number"], timeout=FETCH_TIMEOUT)
    rc, _ = git(repo, "merge-base", "--is-ancestor", tip, sha)
    return rc == 0


def contains(repo: str, target: str, tip: str) -> bool:
    rc, _ = git(repo, "merge-base", "--is-ancestor", tip, target)
    if rc == 0:
        return True

    # Rebase merge: every commit was replayed onto target under a new sha, so
    # match them by patch-id instead.
    cherry = git_out(repo, "cherry", target, tip).splitlines()
    if cherry and all(line.startswith("-") for line in cherry):
        return True

    # Squash merge: target holds one commit with the branch's whole diff.
    # Build that commit (a dangling object gc reclaims) and match its patch-id.
    base = git_out(repo, "merge-base", target, tip)
    tree = git_out(repo, "rev-parse", tip + "^{tree}")
    if not base or not tree or tree == git_out(repo, "rev-parse", base + "^{tree}"):
        return False
    squash = git_out(repo, "commit-tree", "--no-gpg-sign", tree, "-p", base,
                     "-m", "sync-repos", env=SYNTHETIC_ENV)
    if not squash:
        return False
    cherry = git_out(repo, "cherry", target, squash).splitlines()
    return len(cherry) == 1 and cherry[0].startswith("-")


def in_progress(repo: str) -> Optional[str]:
    git_dir = git_out(repo, "rev-parse", "--absolute-git-dir")
    if not git_dir:
        return "could not locate git dir"
    for entry, label in IN_PROGRESS:
        if os.path.exists(os.path.join(git_dir, entry)):
            return label
    return None


def ff_blocker(repo: str) -> Optional[str]:
    """Why the checked-out branch must not be moved, or None if it's safe."""
    blocker = in_progress(repo)
    if blocker:
        return blocker
    rc, out = git(repo, "status", "--porcelain", "--untracked-files=no")
    if rc != 0:
        return "could not read status"
    if out:
        n = len(out.splitlines())
        return "%d uncommitted change%s" % (n, "" if n == 1 else "s")
    return None


# --- discovery --------------------------------------------------------------

def find_repos(root: str) -> List[str]:
    """Every directory under root containing a .git entry.

    A .git directory means a normal repo; a .git file means a submodule or
    linked worktree. Repos are leaves: we never descend into one looking for
    more, but repos in sibling subdirectories are still found.
    """
    repos = []
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        if os.path.exists(os.path.join(dirpath, ".git")):
            repos.append(dirpath)
            dirnames[:] = []
        else:
            dirnames.sort()
    return repos


def dedupe(paths: Iterable[str]) -> Dict[Tuple[int, int], str]:
    out: Dict[Tuple[int, int], str] = {}
    for path in paths:
        st = os.stat(path)
        out.setdefault((st.st_dev, st.st_ino), path)
    return out


def tilde(path: str) -> str:
    home = os.path.realpath(os.path.expanduser("~"))
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def configured_dirs() -> List[str]:
    """The raw directories entries of CONFIG.

    Raises ValueError with a one-line reason naming CONFIG.
    """
    name = tilde(CONFIG)
    try:
        with open(CONFIG) as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        raise ValueError("cannot read %s: %s" % (name, exc.strerror))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = " line %d" % (mark.line + 1) if mark else ""
        problem = getattr(exc, "problem", None) or oneline(str(exc))
        raise ValueError("invalid YAML in %s%s: %s" % (name, where, problem))
    dirs = data.get("directories") if isinstance(data, dict) else None
    if not (isinstance(dirs, list) and dirs and all(isinstance(d, str) for d in dirs)):
        raise ValueError("%s: 'directories' must be a non-empty list of paths" % name)
    return dirs


# --- per-repo work ----------------------------------------------------------

def sync_one(path: str, root: str, modify: bool) -> Repo:
    """Never raises: one pathological repo must not abort the whole run."""
    rel = os.path.relpath(path, root)
    if rel == os.curdir:
        rel = os.path.basename(path)
    try:
        return _sync_one(path, rel, modify)
    except Exception as exc:  # noqa: BLE001 - report and carry on
        return Repo(path=path, rel=rel, ok=False, error="%s: %s" % (type(exc).__name__, exc))


def _sync_one(path: str, rel: str, modify: bool) -> Repo:
    repo = Repo(path=path, rel=rel)

    before = remote_refs(path)
    rc, out = git(path, "fetch", "--all", "--prune", timeout=FETCH_TIMEOUT)
    if rc != 0:
        # Keep going: with several remotes, the ones that did answer produced
        # real refs worth reporting and fast-forwarding.
        repo.ok = False
        repo.error = "git fetch failed: " + (
            oneline(out) or "exited with status %d" % rc
        )
    after = remote_refs(path)

    repo.refs_pruned = sum(1 for ref in before if ref not in after)
    changed = {ref for ref, (sha, _) in after.items() if before.get(ref, ("",))[0] != sha}

    current = git_out(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not current:
        head = git_out(path, "rev-parse", "--short", "HEAD")
        repo.branches.append(
            Branch("HEAD", Action.DETACHED, old=head, current=True)
        )

    # None (pruning off, or worktrees unknown) protects every branch.
    busy = None
    if modify:
        repo.worktrees_pruned = prune_worktrees(path)
        busy = checked_out(path)
    targets = merged_targets(path, after) if busy is not None else {}

    rows = git_out(
        path,
        "for-each-ref",
        "--format=%(refname:short)%09%(objectname)%09%(objectname:short)%09%(upstream)%09%(upstream:short)%09%(upstream:remoteref)",
        "refs/heads",
    )

    explained = set()
    pushed = []
    for line in rows.splitlines():
        name, sha, short, up_ref, up_name, up_head = fields(line, 6)
        if not name:
            continue
        if up_ref in after:
            explained.add(up_ref)
        branch = assess(path, name, sha, short, up_ref, up_name, after,
                        current=name == current, modify=modify)
        repo.branches.append(branch)

        # A branch without a remote upstream was never pushed, so it may be
        # brand new rather than finished; one tracking a default branch is
        # that default branch's local copy.
        if up_ref.startswith("refs/remotes/") and up_ref not in targets:
            pushed.append((branch, up_head[len("refs/heads/"):]))

    if busy is not None and pushed and not targets:
        repo.headless = suggested_remote(path)
    elif busy is not None:
        for branch, head in pushed:
            tidy(path, repo, branch, head, targets, busy)

    if current and not any(b.current for b in repo.branches):
        # HEAD points at a branch that has no ref yet: a fresh `git init` with
        # nothing committed.
        repo.branches.append(Branch(current, Action.UNBORN, current=True))

    repo.refs_new = len(changed - explained)
    repo.branches.sort(key=lambda b: (not b.current, b.name))
    return repo


def assess(path: str, name: str, sha: str, short: str, up_ref: str, up_name: str,
           after: Dict[str, Tuple[str, str]], current: bool, modify: bool) -> Branch:
    """Compare one local branch with its upstream, fast-forwarding if allowed."""
    if not up_ref.startswith("refs/remotes/"):
        return Branch(name, Action.NO_UPSTREAM, old=short, sha=sha, current=current)
    if up_ref not in after:
        return Branch(name, Action.UPSTREAM_GONE, old=short, sha=sha,
                      note="%s no longer exists" % up_name, current=current)

    up_sha, up_short = after[up_ref]
    if up_sha == sha:
        return Branch(name, Action.UP_TO_DATE, old=short, sha=sha, current=current)

    ahead, behind = ahead_behind(path, name, up_ref)
    branch = Branch(name, Action.UP_TO_DATE, old=short, new=up_short,
                    ahead=ahead, behind=behind, current=current, sha=sha)

    if ahead and behind:
        branch.action = Action.DIVERGED
    elif ahead:
        branch.action = Action.AHEAD
    elif behind:
        branch.action = Action.BEHIND if not modify else fast_forward(
            path, branch, up_ref
        )
        if branch.action is Action.FAST_FORWARDED:
            branch.sha = up_sha
    else:
        # Same commit count both ways with different shas: replaced history
        # upstream. Not a fast-forward, so leave it and say so.
        branch.action = Action.DIVERGED
    return branch


def tidy(path: str, repo: Repo, branch: Branch, head: str,
         targets: Dict[str, Target], busy: Dict[str, str]) -> None:
    gone = branch.action is Action.UPSTREAM_GONE
    if not gone and (branch.name in busy or any(
            on_first_parent(path, branch.sha, ref) for ref in targets)):
        return

    proof, why = merge_proof(path, branch.sha, head, targets)
    if not proof:
        if gone:
            branch.action = Action.UNMERGED
            branch.reason = why or "not found in %s" % ", ".join(
                t.short for t in targets.values())
        return

    if branch.action is Action.FAST_FORWARDED:
        branch.old = branch.new
    branch.target, branch.pr = proof.target.short, proof.pr
    if branch.current:
        blocker = switch_away(path, repo, branch, proof.target)
    elif branch.name in busy:
        blocker = "checked out in %s" % tilde(busy[branch.name])
    else:
        blocker = None

    if not blocker:
        rc, out = git(path, "branch", "-D", branch.name)
        if rc != 0:
            blocker = oneline(out, limit=70) or "git branch -D exited with status %d" % rc
    if blocker:
        branch.action, branch.reason = Action.MERGED_KEPT, blocker
    else:
        branch.action = Action.DELETED


def switch_away(path: str, repo: Repo, branch: Branch, target: Target) -> Optional[str]:
    blocker = in_progress(path)
    if blocker:
        return blocker
    local = tracking_branch(path, target)
    if local:
        rc, out = git(path, "switch", local)
    else:
        rc, out = git(path, "switch", "--track", target.short)
    now = local or target.branch
    if rc != 0:
        if "overwritten" in out:
            return "local changes overlap with %s" % now
        if "already used by worktree" in out or "checked out at" in out:
            return "%s is checked out in another worktree" % now
        return oneline(out, limit=70) or "git switch exited with status %d" % rc

    branch.current = False
    branch.switched_to = now
    for other in repo.branches:
        if other.name == now:
            other.current = True
            break
    else:
        repo.branches.append(Branch(now, Action.UP_TO_DATE, current=True,
                                    old=git_out(path, "rev-parse", "--short", "HEAD")))
    return None


def tracking_branch(path: str, target: Target) -> str:
    out = git_out(path, "for-each-ref", "--format=%(refname:short)%09%(upstream)", "refs/heads")
    names = [name for name, up in (fields(line, 2) for line in out.splitlines())
             if up == target.ref]
    if target.branch in names:
        return target.branch
    return names[0] if names else ""


def suggested_remote(path: str) -> str:
    remotes = git_out(path, "remote").split()
    return "origin" if "origin" in remotes or not remotes else remotes[0]


def fast_forward(path: str, branch: Branch, up_ref: str) -> Action:
    """Move branch to its upstream. Returns the resulting Action."""
    if branch.current:
        blocker = ff_blocker(path)
        if blocker:
            branch.note = blocker
            return Action.BLOCKED
        rc, out = git(path, "merge", "--ff-only", up_ref)
    else:
        # git refuses this if it is not a fast-forward, or if the branch is
        # checked out in another worktree.
        rc, out = git(path, "fetch", "--quiet", ".", "%s:refs/heads/%s" % (up_ref, branch.name))

    if rc == 0:
        return Action.FAST_FORWARDED
    if "checked out at" in out:
        # git names the offending worktree path; the actionable part is just
        # that some other worktree owns this branch.
        branch.note = "checked out in another worktree"
    else:
        branch.note = oneline(out, limit=70) or "git exited with status %d" % rc
    return Action.BLOCKED


# --- output -----------------------------------------------------------------

class Palette:
    def __init__(self, enabled: bool):
        self.ok = "\033[32m" if enabled else ""
        self.warn = "\033[33m" if enabled else ""
        self.fail = "\033[31m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.rst = "\033[0m" if enabled else ""

    def paint(self, text: str, color: str) -> str:
        return "%s%s%s" % (color, text, self.rst) if color and text else text


def summarize(branch: Branch) -> Tuple[str, str, str]:
    """(count, range, status) cells for one branch row."""
    a = branch.action
    if a is Action.FAST_FORWARDED:
        return (plural(branch.behind), "%s..%s" % (branch.old, branch.new), "fast-forwarded")
    if a is Action.BLOCKED:
        return (plural(branch.behind), "%s..%s" % (branch.old, branch.new),
                "not fast-forwarded (%s)" % branch.note)
    if a is Action.BEHIND:
        return (plural(branch.behind), "%s..%s" % (branch.old, branch.new), "behind upstream")
    if a is Action.DELETED:
        status = "deleted (%s)" % landed(branch)
        if branch.switched_to:
            status += ", switched to %s" % branch.switched_to
        return ("merged", "was %s" % branch.old, status)
    if a is Action.MERGED_KEPT:
        return ("merged", "at %s" % branch.old,
                "%s, kept: %s" % (landed(branch), branch.reason))
    if a is Action.UNMERGED:
        return ("upstream gone", branch.note, "not merged: %s" % branch.reason)
    if a is Action.DIVERGED:
        return ("diverged", "%d ahead, %d behind" % (branch.ahead, branch.behind), "")
    if a is Action.AHEAD:
        return ("up to date", "%d ahead, unpushed" % branch.ahead, "")
    if a is Action.UPSTREAM_GONE:
        return ("upstream gone", branch.note, "")
    if a is Action.NO_UPSTREAM:
        return ("no upstream", "", "")
    if a is Action.DETACHED:
        return ("detached HEAD", "at %s" % branch.old, "")
    if a is Action.UNBORN:
        return ("no commits yet", "", "")
    return ("up to date", "", "")


def landed(branch: Branch) -> str:
    if branch.pr:
        return "PR #%d into %s" % (branch.pr, branch.target)
    return "in %s" % branch.target


def noun(n: int, one: str, many: str) -> str:
    return "%d %s" % (n, one if n == 1 else many)


def plural(n: int) -> str:
    return "+%d commit%s" % (n, "" if n == 1 else "s")


def repo_tail(repo: Repo) -> str:
    """Activity in this repo that no reported branch accounts for."""
    bits = []
    if repo.refs_new:
        bits.append("%d other remote branch%s updated"
                    % (repo.refs_new, "" if repo.refs_new == 1 else "es"))
    if repo.refs_pruned:
        bits.append("%d stale ref%s pruned"
                    % (repo.refs_pruned, "" if repo.refs_pruned == 1 else "s"))
    if repo.gone:
        bits.append("%d branch%s with deleted upstream"
                    % (repo.gone, "" if repo.gone == 1 else "es"))
    if repo.worktrees_pruned:
        bits.append("%d stale worktree%s pruned"
                    % (repo.worktrees_pruned, "" if repo.worktrees_pruned == 1 else "s"))
    return ", ".join(bits)


def render(repos: List[Repo], p: Palette, elapsed: float) -> None:
    # One row per reported branch: a leading marker plus five aligned columns.
    term = shutil.get_terminal_size((100, 24)).columns
    markers: List[Tuple[str, str]] = []
    rows: List[List[Tuple[str, str]]] = []  # cells of (text, color)
    trailers: Dict[int, List[Tuple[str, str]]] = {}

    for repo in repos:
        if not repo.ok:
            marker, color = "✗", p.fail
        elif repo.flagged:
            marker, color = "~", p.warn
        else:
            marker, color = "✓", p.ok

        shown = [b for b in repo.branches if b.current or b.action in NOTABLE]
        if not shown:
            shown = [Branch("", Action.UP_TO_DATE)]

        for i, branch in enumerate(shown):
            count, rng, status = summarize(branch)
            arrived = branch.action in (Action.FAST_FORWARDED, Action.BLOCKED, Action.BEHIND)
            markers.append((marker, color) if i == 0 else ("", ""))
            rows.append([
                (repo.rel, "") if i == 0 else ("", ""),
                (branch.name, "" if arrived else p.dim),
                (count, p.ok if arrived or branch.action is Action.DELETED else p.dim),
                (rng, p.dim),
                (status, p.warn if branch.flagged else p.dim),
            ])

        tail = repo_tail(repo)
        extra = []
        if tail:
            extra.append((tail, p.dim))
        if repo.headless:
            extra.append(("no remote HEAD, so merged branches were not deleted "
                          "(git remote set-head %s --auto)" % repo.headless, p.dim))
        if repo.error:
            # Clip to one terminal line: a wrapped 3-line git error buries the
            # rest of the report.
            extra.append((oneline(repo.error, limit=max(48, term - 4)), p.fail))
        if extra:
            trailers[len(rows) - 1] = extra

    widths = [max(len(row[i][0]) for row in rows) for i in range(5)] if rows else []

    for n, row in enumerate(rows):
        marker, marker_color = markers[n]
        parts = []
        for i, (text, color) in enumerate(row):
            parts.append(p.paint(text, color) + " " * (widths[i] - len(text)))
        line = "%s %s" % (p.paint(marker, marker_color) or " ", "  ".join(parts))
        print(line.rstrip())
        for text, color in trailers.get(n, []):
            print("    " + p.paint(text, color))

    # Exclusive buckets, most urgent first, so the counts sum to len(repos).
    failed, flagged, updated, behind, quiet = [], [], [], [], []
    for repo in repos:
        if not repo.ok:
            failed.append(repo)
        elif repo.flagged:
            flagged.append(repo)
        elif repo.fast_forwarded or repo.deleted:
            updated.append(repo)
        elif any(b.action is Action.BEHIND for b in repo.branches):
            behind.append(repo)
        else:
            quiet.append(repo)
    ff_branches = sum(len(r.fast_forwarded) for r in repos)
    deleted = sum(len(r.deleted) for r in repos)

    bits = []
    if updated:
        done = []
        if ff_branches:
            done.append(noun(ff_branches, "branch", "branches") + " fast-forwarded")
        if deleted:
            done.append(noun(deleted, "merged branch", "merged branches") + " deleted")
        bits.append(p.paint("%d updated (%s)" % (len(updated), ", ".join(done)), p.ok))
    if quiet:
        bits.append("%d up to date" % len(quiet))
    if behind:
        bits.append("%d behind" % len(behind))
    if flagged:
        bits.append(p.paint("%d need%s attention"
                            % (len(flagged), "s" if len(flagged) == 1 else ""), p.warn))
    if failed:
        bits.append(p.paint("%d failed" % len(failed), p.fail))

    print()
    print("%s: %s (%.1fs)." % (noun(len(repos), "repository", "repositories"),
                               ", ".join(bits) or "nothing to do", elapsed))


def record_deletions(repos: List[Repo]) -> bool:
    """Append every deleted branch to JOURNAL. False if that failed."""
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = ["\t".join((stamp, repo.path, b.name, b.sha, b.target)) + "\n"
             for repo in repos for b in repo.deleted]
    if not lines:
        return True
    try:
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        with open(JOURNAL, "a") as f:
            f.writelines(lines)
    except OSError as exc:
        # The shas below are the only way back to these branches, so they
        # must land somewhere: the log keeps them until the next run.
        print("sync-repos: cannot append to %s: %s"
              % (tilde(JOURNAL), exc.strerror), file=sys.stderr)
        sys.stderr.writelines(lines)
        return False
    return True


# --- main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sync-repos",
        description="Fetch every git repo under each DIR in parallel, "
                    "fast-forward local branches that can move safely, and "
                    "delete local branches already merged into a remote's "
                    "default branch, switching off one that is checked out.",
    )
    parser.add_argument("dirs", nargs="*", metavar="DIR",
                        help="directory to scan (default: the directories in %s)"
                        % tilde(CONFIG))
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                        help="number of parallel fetches (default: CPU count)")
    parser.add_argument("-n", "--fetch-only", action="store_true",
                        help="only fetch; never fast-forward, switch, or delete a local branch")
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("-j must be a positive integer")
    for d in args.dirs:
        if not os.path.isdir(d):
            parser.error("not a directory: %s" % d)

    dirs = args.dirs
    if not dirs:
        try:
            entries = configured_dirs()
        except ValueError as exc:
            print("sync-repos: %s" % exc, file=sys.stderr)
            return 2
        for entry in entries:
            path = os.path.expandvars(os.path.expanduser(entry))
            if os.path.isdir(path):
                dirs.append(path)
            else:
                print("skipping %s: not a directory" % entry, file=sys.stderr)
        if not dirs:
            print("sync-repos: no directory listed in %s exists"
                  % tilde(CONFIG), file=sys.stderr)
            return 2

    # Keyed by inode, not path: nested roots, or two spellings of one directory
    # on a case-insensitive volume, would otherwise sync the same repo twice.
    roots = list(dedupe(os.path.realpath(d) for d in dirs).values())
    repos = list(dedupe(r for root in roots for r in find_repos(root)).values())
    base = os.path.commonpath(roots)
    under = ", ".join(tilde(root) for root in roots)
    if not repos:
        print("No git repositories found under %s" % under)
        return 0

    modify = not args.fetch_only
    mode = "" if modify else ", fetch only"
    print("Syncing %s under %s (%d parallel%s)..."
          % (noun(len(repos), "repository", "repositories"), under, args.jobs, mode))
    print()

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda path: sync_one(path, base, modify), repos))
    elapsed = time.monotonic() - started

    results.sort(key=lambda r: r.rel)
    recorded = record_deletions(results)
    render(results, Palette(sys.stdout.isatty() and not os.environ.get("NO_COLOR")), elapsed)
    return 1 if not recorded or any(not r.ok for r in results) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
