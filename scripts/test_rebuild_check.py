import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "rebuild-check.py")
REPO = os.path.realpath(os.path.dirname(HERE))
NIX_PATH_LITERAL = re.compile(re.escape(REPO) + r"/([\w.+\-/]+)")

NOT_APPLIED = "my-setup: the active setup differs from origin/main. Run ~/.my-setup/rebuild.sh\n"
PULL = ("my-setup: the active setup differs from origin/main. "
        "Run git -C ~/.my-setup pull, then ~/.my-setup/rebuild.sh\n")
UNRECORDED = "my-setup: the active setup was not recorded by rebuild.sh. Run ~/.my-setup/rebuild.sh\n"
NO_RECORD = "my-setup: no rebuild recorded. Run ~/.my-setup/rebuild.sh\n"
ONE_CHANGE = "my-setup: 1 uncommitted change. Run git -C ~/.my-setup status\n"
TWO_CHANGES = "my-setup: 2 uncommitted changes. Run git -C ~/.my-setup status\n"
THREE_CHANGES = "my-setup: 3 uncommitted changes. Run git -C ~/.my-setup status\n"

SEED = {
    "flake.nix": "{ }\n",
    "home.nix": "{ zsh = true; }\n",
    ".gitignore": ".state/\n",
    "README.md": "# my-setup\n",
    "LICENSE": "MIT\n",
    "home/.config/sync-repos/config.yaml": "directories:\n  - ~/Codes\n",
}


def load_script():
    spec = importlib.util.spec_from_file_location("rebuild_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class World:
    def __init__(self, tmp: str):
        self.tmp = tmp
        self.origin = os.path.join(tmp, "origin.git")
        self.upstream = os.path.join(tmp, "upstream")
        self.checkout = os.path.join(tmp, ".my-setup")
        self.state = os.path.join(self.checkout, ".state", "rebuild-check.json")
        self.current = os.path.join(tmp, "current-system")
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": tmp,
            "REBUILD_CHECK_CURRENT_SYSTEM": self.current,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        self.boot("a")

    def system(self, name: str) -> str:
        path = os.path.join(self.tmp, "systems", name)
        os.makedirs(path, exist_ok=True)
        return path

    def boot(self, name: str) -> None:
        target = self.system(name)
        if os.path.lexists(self.current):
            os.remove(self.current)
        os.symlink(target, self.current)

    def git(self, cwd: str, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env,
                              capture_output=True, text=True, check=True).stdout

    def write(self, root: str, path: str, content: str) -> None:
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def clone(self) -> None:
        os.makedirs(self.upstream)
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        self.git(self.upstream, "init", "-q", "-b", "main")
        for path, content in SEED.items():
            self.write(self.upstream, path, content)
        self.git(self.upstream, "add", "-A")
        self.git(self.upstream, "commit", "-q", "-m", "seed")
        self.git(self.upstream, "remote", "add", "origin", self.origin)
        self.git(self.upstream, "push", "-q", "-u", "origin", "main")
        self.git(self.tmp, "clone", "-q", self.origin, self.checkout)

    def push(self, path: str, content: str) -> None:
        self.write(self.upstream, path, content)
        self.git(self.upstream, "commit", "-q", "-am", "change %s" % path)
        self.git(self.upstream, "push", "-q")

    def fetch(self) -> None:
        self.git(self.checkout, "fetch", "-q")

    def pull(self) -> None:
        self.git(self.checkout, "pull", "-q", "--ff-only")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, SCRIPT, *args], env=self.env,
                              capture_output=True, text=True)


class RebuildCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="rebuild-check-test-"))
        self.addCleanup(shutil.rmtree, tmp)
        self.w = World(tmp)

    def assertRan(self, p: subprocess.CompletedProcess, code: int,
                  stdout: str, stderr: str = "") -> None:
        self.assertEqual((p.returncode, p.stdout, p.stderr), (code, stdout, stderr))

    def recorded(self) -> None:
        self.w.clone()
        self.assertRan(self.w.run("--record", self.w.checkout), 0, "")

    def test_record_writes_the_active_setup_and_a_check_right_after_is_silent(self) -> None:
        w = self.w
        self.recorded()

        with open(w.state) as f:
            state = json.load(f)
        self.assertEqual(sorted(state), ["hash", "system"])
        self.assertEqual(state["system"], os.path.join(w.tmp, "systems", "a"))
        self.assertEqual(os.listdir(os.path.dirname(w.state)), ["rebuild-check.json"])
        self.assertEqual([n for n in os.listdir(os.path.join(w.checkout, ".git")) if n.startswith("rebuild-check")], [])
        self.assertRan(w.run(), 0, "")

    def test_changes_only_to_excluded_files_on_origin_main_are_silent(self) -> None:
        w = self.w
        self.recorded()
        w.push("README.md", "# my-setup, now with docs\n")
        w.push("LICENSE", "MIT, updated\n")
        w.push("home/.config/sync-repos/config.yaml", "directories:\n  - ~/src\n")
        w.pull()

        self.assertRan(w.run(), 0, "")

    def test_a_config_change_on_origin_main_asks_for_a_rebuild(self) -> None:
        w = self.w
        self.recorded()
        w.push("home.nix", "{ zsh = false; }\n")
        w.pull()

        self.assertRan(w.run(), 0, NOT_APPLIED)

    def test_a_config_change_not_yet_pulled_asks_for_a_pull_first(self) -> None:
        w = self.w
        self.recorded()
        w.push("home.nix", "{ zsh = false; }\n")
        w.fetch()

        self.assertRan(w.run(), 0, PULL)

    def test_an_active_setup_other_than_the_recorded_one_asks_for_a_rebuild(self) -> None:
        w = self.w
        self.recorded()
        w.boot("b")

        self.assertRan(w.run(), 0, UNRECORDED)

    def test_no_recorded_rebuild_asks_for_a_rebuild(self) -> None:
        w = self.w
        w.clone()

        self.assertRan(w.run(), 0, NO_RECORD)

    def test_a_malformed_state_file_is_silent(self) -> None:
        w = self.w
        self.recorded()
        w.push("home.nix", "{ zsh = false; }\n")
        w.pull()
        with open(w.state, "w") as f:
            f.write('{"system": 1}')

        self.assertRan(w.run(), 0, "")

    def test_a_missing_checkout_is_silent(self) -> None:
        self.assertRan(self.w.run(), 0, "")

    def test_a_checkout_directory_that_is_not_a_repo_is_silent(self) -> None:
        os.makedirs(self.w.checkout)

        self.assertRan(self.w.run(), 0, "")

    def test_a_repo_without_origin_main_is_silent(self) -> None:
        w = self.w
        os.makedirs(w.checkout)
        w.git(w.checkout, "init", "-q", "-b", "main")
        w.write(w.checkout, "home.nix", "{ }\n")
        w.git(w.checkout, "add", "-A")
        w.git(w.checkout, "commit", "-q", "-m", "seed")

        self.assertRan(w.run(), 0, "")

    def test_record_without_a_repo_fails_and_writes_nothing(self) -> None:
        w = self.w
        p = w.run("--record", w.checkout)

        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertRegex(p.stderr, r"^rebuild-check: .*\.my-setup")
        self.assertFalse(os.path.exists(w.checkout))

    def test_record_hashes_uncommitted_edits_that_later_land_on_origin_main(self) -> None:
        w = self.w
        w.clone()
        w.write(w.checkout, "home.nix", "{ zsh = false; }\n")
        w.write(w.checkout, "flake.nix", "{ inputs = { }; }\n")
        w.git(w.checkout, "add", "flake.nix")
        self.assertRan(w.run("--record", w.checkout), 0, "")

        self.assertRan(w.run(), 0, NOT_APPLIED + TWO_CHANGES)

        w.push("home.nix", "{ zsh = false; }\n")
        w.push("flake.nix", "{ inputs = { }; }\n")
        w.fetch()

        self.assertRan(w.run(), 0, TWO_CHANGES)

    def test_record_from_a_worktree_hashes_that_worktree_into_the_main_checkouts_state(self) -> None:
        w = self.w
        w.clone()
        worktree = os.path.join(w.tmp, "worktree")
        w.git(w.checkout, "worktree", "add", "-q", "-b", "try", worktree)
        w.write(worktree, "home.nix", "{ zsh = false; }\n")

        self.assertRan(w.run("--record", worktree), 0, "")
        self.assertTrue(os.path.exists(w.state))
        self.assertFalse(os.path.exists(os.path.join(worktree, ".state")))
        self.assertRan(w.run(), 0, NOT_APPLIED)

        w.push("home.nix", "{ zsh = false; }\n")
        w.fetch()

        self.assertRan(w.run(), 0, "")

    def test_record_leaves_the_real_index_untouched(self) -> None:
        w = self.w
        w.clone()
        w.write(w.checkout, "flake.nix", "{ inputs = { }; }\n")
        w.git(w.checkout, "add", "flake.nix")
        w.write(w.checkout, "home.nix", "{ zsh = false; }\n")
        w.write(w.checkout, "notes.txt", "scratch\n")
        before = (w.git(w.checkout, "diff", "--cached"), w.git(w.checkout, "status", "--porcelain"))

        self.assertRan(w.run("--record", w.checkout), 0, "")

        self.assertEqual((w.git(w.checkout, "diff", "--cached"),
                          w.git(w.checkout, "status", "--porcelain")), before)
        self.assertEqual(before[1], "M  flake.nix\n M home.nix\n?? notes.txt\n")

    def test_untracked_files_do_not_affect_the_recorded_hash(self) -> None:
        w = self.w
        w.clone()
        w.write(w.checkout, "notes.txt", "scratch\n")
        w.write(w.checkout, "scripts/new-tool.py", "print('hi')\n")
        self.assertRan(w.run("--record", w.checkout), 0, "")

        self.assertRan(w.run(), 0, TWO_CHANGES)

    def test_a_modified_tracked_file_in_a_current_checkout_reports_only_the_uncommitted_change(self) -> None:
        w = self.w
        self.recorded()
        w.write(w.checkout, "home.nix", "{ zsh = false; }\n")

        self.assertRan(w.run(), 0, ONE_CHANGE)

    def test_an_untracked_file_is_an_uncommitted_change(self) -> None:
        w = self.w
        self.recorded()
        w.write(w.checkout, "notes.txt", "scratch\n")

        self.assertRan(w.run(), 0, ONE_CHANGE)

    def test_each_file_in_a_new_directory_is_its_own_uncommitted_change(self) -> None:
        w = self.w
        self.recorded()
        w.write(w.checkout, "drafts/one.txt", "1\n")
        w.write(w.checkout, "drafts/two.txt", "2\n")

        self.assertRan(w.run(), 0, TWO_CHANGES)

    def test_a_staged_change_is_an_uncommitted_change(self) -> None:
        w = self.w
        self.recorded()
        w.write(w.checkout, "flake.nix", "{ inputs = { }; }\n")
        w.git(w.checkout, "add", "flake.nix")

        self.assertRan(w.run(), 0, ONE_CHANGE)

    def test_gitignored_files_are_not_uncommitted_changes(self) -> None:
        w = self.w
        self.recorded()
        w.write(w.checkout, ".state/scratch.txt", "scratch\n")
        w.write(w.checkout, "notes.txt", "scratch\n")

        self.assertRan(w.run(), 0, ONE_CHANGE)

    def test_modified_staged_and_untracked_files_count_as_separate_changes(self) -> None:
        w = self.w
        self.recorded()
        w.write(w.checkout, "home.nix", "{ zsh = false; }\n")
        w.write(w.checkout, "flake.nix", "{ inputs = { }; }\n")
        w.git(w.checkout, "add", "flake.nix")
        w.write(w.checkout, "notes.txt", "scratch\n")

        self.assertRan(w.run(), 0, THREE_CHANGES)

    def test_a_staged_rename_is_one_uncommitted_change(self) -> None:
        w = self.w
        self.recorded()
        w.git(w.checkout, "mv", "README.md", "README.txt")

        self.assertRan(w.run(), 0, ONE_CHANGE)

    def test_a_stale_system_and_a_dirty_checkout_report_both_in_order(self) -> None:
        w = self.w
        self.recorded()
        w.push("home.nix", "{ zsh = false; }\n")
        w.pull()
        w.write(w.checkout, "README.md", "# my-setup, draft\n")

        self.assertRan(w.run(), 0, NOT_APPLIED + ONE_CHANGE)

    def test_a_dirty_repo_without_origin_main_reports_the_uncommitted_change(self) -> None:
        w = self.w
        os.makedirs(w.checkout)
        w.git(w.checkout, "init", "-q", "-b", "main")
        w.write(w.checkout, "home.nix", "{ }\n")
        w.git(w.checkout, "add", "-A")
        w.git(w.checkout, "commit", "-q", "-m", "seed")
        w.write(w.checkout, "home.nix", "{ zsh = true; }\n")

        self.assertRan(w.run(), 0, ONE_CHANGE)


class ExcludeListTest(unittest.TestCase):
    def test_the_state_file_is_gitignored_in_this_repo(self) -> None:
        state = os.path.relpath(load_script().STATE, load_script().REPO)
        p = subprocess.run(["git", "-C", REPO, "check-ignore", "-q", state])

        self.assertEqual(p.returncode, 0, "%s must be in .gitignore" % state)

    def test_no_file_a_nix_file_reads_by_path_is_excluded(self) -> None:
        script = load_script()
        repo_files = subprocess.run(["git", "-C", REPO, "ls-files", "-z", "-co", "--exclude-standard"],
                                    capture_output=True, text=True, check=True).stdout.split("\0")
        references = set()
        for path in repo_files:
            if path.endswith(".nix"):
                ast = subprocess.run(["nix-instantiate", "--parse", os.path.join(REPO, path)],
                                     capture_output=True, text=True, check=True).stdout
                references.update(NIX_PATH_LITERAL.findall(ast))
        self.assertIn("scripts/rebuild-check.py", references)
        for ref in sorted(references):
            files = [p for p in repo_files if p == ref or p.startswith(ref + "/")]
            self.assertTrue(files, "%s is referenced by a .nix file but not in the repo" % ref)
            self.assertEqual([p for p in files if script.excluded(p)], [],
                             "%s is read by a .nix file, so it must not be in EXCLUDED" % ref)


if __name__ == "__main__":
    unittest.main()
