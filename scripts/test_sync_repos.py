import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync-repos.py")
URL = "https://github.com/acme/widget.git"

FAKE_GH = """#!%s
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
repo = args[args.index("--repo") + 1]
head = args[args.index("--head") + 1]
with open(os.environ["FAKE_GH_PRS"]) as f:
    prs = json.load(f)
print(json.dumps([{k: v for k, v in pr.items() if k not in ("repo", "head")}
                  for pr in prs if pr["repo"] == repo and pr["head"] == head]))
"""


class World:
    def __init__(self, tmp: str):
        self.tmp = tmp
        self.root = os.path.join(tmp, "code")
        self.remote = os.path.join(tmp, "remote.git")
        self.state = os.path.join(tmp, "state")
        self.prs_file = os.path.join(tmp, "prs.json")
        self.gh_log = os.path.join(tmp, "gh.log")
        bindir = os.path.join(tmp, "bin")
        os.makedirs(self.root)
        os.makedirs(bindir)
        with open(os.path.join(bindir, "gh"), "w") as f:
            f.write(FAKE_GH % sys.executable)
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        gitconfig = os.path.join(tmp, "gitconfig")
        with open(gitconfig, "w") as f:
            f.write("[user]\n\tname = t\n\temail = t@t\n"
                    "[init]\n\tdefaultBranch = main\n"
                    "[advice]\n\tdetachedHead = false\n"
                    '[url "%s"]\n\tinsteadOf = %s\n' % (self.remote, URL))
        self.prs = []
        self.save_prs()
        self.env = {
            "PATH": "%s:/usr/bin:/bin" % bindir,
            "HOME": tmp,
            "XDG_STATE_HOME": self.state,
            "GIT_CONFIG_GLOBAL": gitconfig,
            "GIT_CONFIG_NOSYSTEM": "1",
            "FAKE_GH_PRS": self.prs_file,
            "FAKE_GH_LOG": self.gh_log,
        }
        self.git(tmp, "init", "-q", "--bare", self.remote)
        self.hub = os.path.join(tmp, "hub")
        self.git(tmp, "init", "-q", self.hub)
        self.git(self.hub, "remote", "add", "origin", URL)
        self.write(self.hub, "f", "".join("%d\n" % i for i in range(1, 21)))
        self.commit(self.hub, "base")
        self.git(self.hub, "push", "-q", "-u", "origin", "main")
        self.local = os.path.join(self.root, "widget")
        self.git(tmp, "clone", "-q", URL, self.local)

    def git(self, cwd: str, *args: str) -> str:
        p = subprocess.run(["git", *args], cwd=cwd, env=self.env,
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), p.stderr))
        return p.stdout.strip()

    def write(self, cwd: str, name: str, text: str) -> None:
        with open(os.path.join(cwd, name), "w") as f:
            f.write(text)

    def read(self, cwd: str, name: str) -> str:
        with open(os.path.join(cwd, name)) as f:
            return f.read()

    def edit_line(self, cwd: str, old: str, new: str) -> None:
        lines = self.read(cwd, "f").splitlines()
        self.write(cwd, "f", "".join("%s\n" % (new if line == old else line) for line in lines))

    def commit(self, cwd: str, msg: str) -> str:
        self.git(cwd, "add", "-A")
        self.git(cwd, "commit", "-q", "-m", msg)
        return self.git(cwd, "rev-parse", "HEAD")

    def save_prs(self) -> None:
        with open(self.prs_file, "w") as f:
            json.dump(self.prs, f)

    def pr(self, number: int, head: str, sha: str, state: str = "MERGED") -> None:
        self.git(self.remote, "update-ref", "refs/pull/%d/head" % number, sha)
        self.prs.append({"repo": "acme/widget", "head": head, "number": number,
                         "state": state, "headRefOid": sha, "baseRefName": "main"})
        self.save_prs()

    def feature(self, name: str, old: str, new: str) -> str:
        self.git(self.local, "switch", "-q", "-c", name, "origin/main")
        self.edit_line(self.local, old, new)
        sha = self.commit(self.local, name)
        self.git(self.local, "push", "-q", "-u", "origin", name)
        self.git(self.local, "switch", "-q", "main")
        return sha

    def land_on_main(self, msg: str, old: str, new: str) -> None:
        self.git(self.hub, "pull", "-q", "--ff-only", "origin", "main")
        self.edit_line(self.hub, old, new)
        self.commit(self.hub, msg)
        self.git(self.hub, "push", "-q", "origin", "main")

    def squash_merge(self, branch: str, delete: bool = True) -> None:
        self.git(self.hub, "fetch", "-q", "origin")
        self.git(self.hub, "switch", "-q", "main")
        self.git(self.hub, "merge", "-q", "--ff-only", "origin/main")
        self.git(self.hub, "merge", "-q", "--squash", "origin/" + branch)
        self.git(self.hub, "commit", "-q", "-m", "squash " + branch)
        self.git(self.hub, "push", "-q", "origin", "main")
        if delete:
            self.git(self.hub, "push", "-q", "origin", "--delete", branch)

    def sync(self, *flags: str) -> str:
        p = subprocess.run([sys.executable, SCRIPT, *flags, self.root], env=self.env,
                           capture_output=True, text=True)
        return p.stdout + p.stderr

    def branches(self) -> list:
        return self.git(self.local, "for-each-ref", "--format=%(refname:short)",
                        "refs/heads").splitlines()

    def current(self) -> str:
        return self.git(self.local, "symbolic-ref", "--short", "HEAD")


class SyncReposTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="sync-repos-test-")
        self.addCleanup(shutil.rmtree, tmp)
        self.w = World(tmp)

    def test_squash_merge_beside_a_nearby_main_edit_is_proven_by_github(self) -> None:
        w = self.w
        sha = w.feature("feat/a", "10", "ten")
        w.land_on_main("nearby", "8", "eight")
        w.squash_merge("feat/a")
        w.pr(7, "feat/a", sha)

        out = w.sync()

        self.assertEqual(w.branches(), ["main"])
        self.assertIn("deleted (PR #7 into origin/main)", out)

    def test_pr_head_with_commits_never_pulled_still_proves_the_merge(self) -> None:
        w = self.w
        w.feature("feat/b", "10", "ten")
        w.git(w.hub, "fetch", "-q", "origin")
        w.git(w.hub, "switch", "-q", "-c", "feat/b", "origin/feat/b")
        w.edit_line(w.hub, "12", "twelve")
        review = w.commit(w.hub, "review fix")
        w.git(w.hub, "push", "-q", "origin", "feat/b")
        w.git(w.hub, "switch", "-q", "main")
        w.squash_merge("feat/b")
        w.pr(8, "feat/b", review)

        out = w.sync()

        self.assertEqual(w.branches(), ["main"])
        self.assertIn("deleted (PR #8 into origin/main)", out)

    def test_merged_current_branch_switches_to_main_carrying_uncommitted_changes(self) -> None:
        w = self.w
        w.feature("feat/c", "10", "ten")
        w.squash_merge("feat/c")
        w.git(w.local, "switch", "-q", "feat/c")
        w.write(w.local, "notes", "wip\n")
        w.git(w.local, "add", "notes")

        out = w.sync()

        self.assertEqual(w.current(), "main")
        self.assertEqual(w.branches(), ["main"])
        self.assertEqual(w.read(w.local, "notes"), "wip\n")
        self.assertIn("ten\n", w.read(w.local, "f"))
        self.assertIn("deleted (in origin/main), switched to main", out)

    def test_merged_current_branch_stays_when_local_changes_overlap_main(self) -> None:
        w = self.w
        w.feature("feat/d", "10", "ten")
        w.squash_merge("feat/d")
        w.land_on_main("later", "3", "three")
        w.git(w.local, "switch", "-q", "feat/d")
        w.edit_line(w.local, "3", "mine")

        out = w.sync()

        self.assertEqual(w.current(), "feat/d")
        self.assertIn("feat/d", w.branches())
        self.assertIn("mine\n", w.read(w.local, "f"))
        self.assertIn("in origin/main, kept: local changes overlap with main", out)

    def test_pushed_branch_without_own_commits_is_kept_after_main_moves(self) -> None:
        w = self.w
        w.git(w.local, "switch", "-q", "-c", "feat/e")
        w.git(w.local, "push", "-q", "-u", "origin", "feat/e")
        w.git(w.local, "switch", "-q", "-c", "feat/f")
        w.git(w.local, "push", "-q", "-u", "origin", "feat/f")
        w.write(w.local, "f", "edited\n")
        w.land_on_main("moves on", "5", "five")

        w.sync()

        self.assertEqual(w.current(), "feat/f")
        self.assertEqual(w.branches(), ["feat/e", "feat/f", "main"])
        self.assertEqual(w.read(w.local, "f"), "edited\n")

    def test_merged_branch_whose_remote_branch_was_never_deleted_is_deleted(self) -> None:
        w = self.w
        w.feature("feat/g", "10", "ten")
        w.git(w.hub, "fetch", "-q", "origin")
        w.git(w.hub, "merge", "-q", "--no-ff", "-m", "merge feat/g", "origin/feat/g")
        w.git(w.hub, "push", "-q", "origin", "main")

        out = w.sync()

        self.assertEqual(w.branches(), ["main"])
        self.assertIn("deleted (in origin/main)", out)

    def test_gone_branch_from_a_closed_pr_is_kept_and_flagged(self) -> None:
        w = self.w
        sha = w.feature("feat/h", "10", "ten")
        w.git(w.hub, "push", "-q", "origin", "--delete", "feat/h")
        w.pr(9, "feat/h", sha, state="CLOSED")

        out = w.sync()

        self.assertIn("feat/h", w.branches())
        self.assertIn("not merged: PR #9 closed without merging", out)
        self.assertIn("1 needs attention", out)

    def test_never_pushed_branch_is_untouched_and_never_asks_github(self) -> None:
        w = self.w
        w.git(w.local, "switch", "-q", "-c", "local-only", "--track", "main")
        w.edit_line(w.local, "10", "ten")
        w.commit(w.local, "local")

        out = w.sync()

        self.assertEqual(w.current(), "local-only")
        self.assertIn("local-only", w.branches())
        self.assertIn("no upstream", out)
        self.assertNotIn("upstream gone", out)
        self.assertFalse(os.path.exists(w.gh_log))

    def test_without_gh_patch_id_still_works_and_the_reason_is_shown(self) -> None:
        w = self.w
        w.env["PATH"] = "/usr/bin:/bin"
        w.feature("feat/i", "10", "ten")
        w.squash_merge("feat/i")
        w.feature("feat/j", "15", "fifteen")
        w.land_on_main("nearby", "13", "thirteen")
        w.squash_merge("feat/j")

        out = w.sync()

        self.assertEqual(w.branches(), ["feat/j", "main"])
        self.assertIn("deleted (in origin/main)", out)
        self.assertIn("not merged: gh not found", out)

    def test_stale_worktree_record_is_pruned_so_its_branch_can_go(self) -> None:
        w = self.w
        w.feature("feat/k", "10", "ten")
        tree = os.path.join(w.tmp, "tree")
        w.git(w.local, "worktree", "add", "-q", tree, "feat/k")
        shutil.rmtree(tree)
        w.squash_merge("feat/k")

        out = w.sync()

        self.assertEqual(w.branches(), ["main"])
        self.assertEqual(w.git(w.local, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertIn("1 stale worktree pruned", out)

    def test_merged_branch_in_a_live_worktree_is_kept_and_named(self) -> None:
        w = self.w
        w.feature("feat/l", "10", "ten")
        tree = os.path.join(w.tmp, "tree")
        w.git(w.local, "worktree", "add", "-q", tree, "feat/l")
        w.squash_merge("feat/l")

        out = w.sync()

        self.assertIn("feat/l", w.branches())
        self.assertIn("in origin/main, kept: checked out in ~/tree", out)

    def test_fetch_only_neither_switches_nor_deletes(self) -> None:
        w = self.w
        w.feature("feat/m", "10", "ten")
        w.squash_merge("feat/m")
        w.git(w.local, "switch", "-q", "feat/m")

        w.sync("-n")

        self.assertEqual(w.current(), "feat/m")
        self.assertEqual(w.branches(), ["feat/m", "main"])


if __name__ == "__main__":
    unittest.main()
