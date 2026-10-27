from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from typing import Callable, Dict, NamedTuple, Optional, Tuple

EXCLUDED = (
    "README.md",
    "LICENSE",
    ".gitignore",
    ".github/*",
    "bootstrap.sh",
    "rebuild.sh",
    "scripts/test_*.py",
    "home/.config/*",
)


def active_setup_link() -> str:
    if sys.platform == "darwin":
        return "/run/current-system"
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "nix", "profiles", "home-manager")


REPO = os.path.expanduser("~/.my-setup")
STATE = os.path.join(REPO, ".state", "rebuild-check.json")
CURRENT_SYSTEM = os.environ.get("REBUILD_CHECK_CURRENT_SYSTEM") or active_setup_link()
ORIGIN_MAIN = "refs/remotes/origin/main"
LOCAL_MAIN = "refs/heads/main"
GIT_TIMEOUT = 2.0

NO_RECORD = "no rebuild recorded"
UNRECORDED = "the active setup was not recorded by rebuild.sh"
NOT_APPLIED = "the active setup differs from origin/main"
REBUILD = "~/.my-setup/rebuild.sh"
PULL_AND_REBUILD = "git -C ~/.my-setup pull, then ~/.my-setup/rebuild.sh"
STATUS = "git -C ~/.my-setup status"


class GitError(Exception):
    pass


class StateError(Exception):
    pass


class Applied(NamedTuple):
    system: str
    hash: str


def git(repo: str, *args: str, env: Optional[Dict[str, str]] = None) -> bytes:
    try:
        p = subprocess.run(["git", "-C", repo, *args], env=env,
                           capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(str(exc))
    if p.returncode != 0:
        detail = p.stderr.decode(errors="replace").strip()
        raise GitError(detail or "git %s exited with status %d" % (args[0], p.returncode))
    return p.stdout


def excluded(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in EXCLUDED)


def config_hash(repo: str, tree: str) -> str:
    digest = hashlib.sha256()
    for entry in git(repo, "ls-tree", "-r", "-z", "--full-tree", tree).split(b"\0"):
        if entry and not excluded(os.fsdecode(entry.split(b"\t", 1)[1])):
            digest.update(entry + b"\0")
    return digest.hexdigest()


def git_path(repo: str, name: str) -> str:
    return os.path.join(repo, os.fsdecode(git(repo, "rev-parse", "--git-path", name).strip()))


def worktree_tree(repo: str) -> str:
    index = git_path(repo, "index")
    scratch = git_path(repo, "rebuild-check-index.%d" % os.getpid())
    with open(index, "rb") as src, open(scratch, "wb") as dst:
        dst.write(src.read())
    try:
        env = {**os.environ, "GIT_INDEX_FILE": scratch}
        git(repo, "add", "-u", env=env)
        return git(repo, "write-tree", env=env).decode().strip()
    finally:
        os.remove(scratch)


def current_system() -> str:
    return os.path.realpath(CURRENT_SYSTEM)


def read_applied() -> Optional[Applied]:
    try:
        with open(STATE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise StateError(str(exc))
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("system", "hash")):
        raise StateError("%s is not a rebuild-check state file" % STATE)
    return Applied(data["system"], data["hash"])


def write_applied(applied: Applied) -> None:
    tmp = "%s.%d" % (STATE, os.getpid())
    with open(tmp, "w") as f:
        json.dump({"system": applied.system, "hash": applied.hash}, f, indent=2)
        f.write("\n")
    os.replace(tmp, STATE)


def stale_reason(applied: Optional[Applied], system: str, wanted: str) -> Optional[str]:
    if applied is None:
        return NO_RECORD
    if applied.system != system:
        return UNRECORDED
    if applied.hash != wanted:
        return NOT_APPLIED
    return None


def behind() -> bool:
    return int(git(REPO, "rev-list", "--count", "%s..%s" % (LOCAL_MAIN, ORIGIN_MAIN))) > 0


def stale() -> Optional[str]:
    wanted = config_hash(REPO, ORIGIN_MAIN)
    reason = stale_reason(read_applied(), current_system(), wanted)
    if reason is None:
        return None
    return "my-setup: %s. Run %s" % (reason, PULL_AND_REBUILD if behind() else REBUILD)


def dirty() -> Optional[str]:
    entries = iter(git(REPO, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all").split(b"\0"))
    count = 0
    for entry in entries:
        if entry:
            count += 1
            if b"R" in entry[:2] or b"C" in entry[:2]:
                next(entries)
    if count == 0:
        return None
    return "my-setup: %d uncommitted %s. Run %s" % (count, "change" if count == 1 else "changes", STATUS)


CHECKS: Tuple[Callable[[], Optional[str]], ...] = (stale, dirty)


def record(repo: str) -> None:
    applied = Applied(current_system(), config_hash(repo, worktree_tree(repo)))
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    write_applied(applied)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="rebuild-check",
        description="Say when the active setup was not built from "
                    "the config on origin/main, and when the checkout has uncommitted changes.",
    )
    parser.add_argument("--record", metavar="CHECKOUT",
                        help="record the working tree of CHECKOUT as applied to the active setup")
    args = parser.parse_args()
    if args.record is not None:
        try:
            record(args.record)
        except (GitError, OSError) as exc:
            print("rebuild-check: %s" % exc, file=sys.stderr)
            return 1
        return 0
    for check in CHECKS:
        try:
            message = check()
        except (GitError, StateError):
            continue
        if message is not None:
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
