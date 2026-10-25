import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure lib and bin are in path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

try:
    from kitcode_core import (
        GitRepoState,
        KittyController,
        PreviewState,
        PreviewTab,
        SearchMatch,
        compute_tree_fingerprint,
        fuzzy_find_files,
        get_git_status,
        ripgrep_search,
        KitcodePreviewApp,
        KitcodeTreeApp,
    )
except ImportError as e:
    # During RED phase, allow test module to load or fail as expected
    pass


class TestKitcodeGit(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True, capture_output=True)

        # Commit an initial file
        (self.root / "initial.txt").write_text("hello world")
        subprocess.run(["git", "add", "initial.txt"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.root, check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_git_status_clean(self):
        state = get_git_status(self.root)
        self.assertEqual(state.branch, "main")
        self.assertEqual(state.file_badges, {})
        self.assertEqual(state.dirty_dirs, set())
        self.assertEqual(state.dirty_count, 0)

    def test_git_status_dirty_and_badges(self):
        # Modify initial.txt -> M
        (self.root / "initial.txt").write_text("hello modified")

        # Create subfolder with untracked file -> U
        sub = self.root / "src" / "pkg"
        sub.mkdir(parents=True)
        untracked = sub / "new_file.py"
        untracked.write_text("print('hello')")

        # Create staged file -> A
        staged = self.root / "staged.txt"
        staged.write_text("staged content")
        subprocess.run(["git", "add", "staged.txt"], cwd=self.root, check=True, capture_output=True)

        state = get_git_status(self.root)
        self.assertEqual(state.branch, "main")
        self.assertEqual(state.file_badges.get((self.root / "initial.txt").resolve()), "M")
        self.assertEqual(state.file_badges.get(untracked.resolve()), "U")
        self.assertEqual(state.file_badges.get(staged.resolve()), "A")
        self.assertIn(sub.resolve(), state.dirty_dirs)
        self.assertIn((self.root / "src").resolve(), state.dirty_dirs)
        self.assertGreaterEqual(state.dirty_count, 3)

    def test_git_status_non_git(self):
        non_git = Path(tempfile.mkdtemp())
        try:
            state = get_git_status(non_git)
            self.assertIsNone(state.branch)
            self.assertEqual(state.file_badges, {})
            self.assertEqual(state.dirty_dirs, set())
            self.assertEqual(state.dirty_count, 0)
        finally:
            shutil.rmtree(non_git)


class TestTreeFingerprint(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        (self.root / "file1.txt").write_text("one")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_compute_tree_fingerprint(self):
        fp1 = compute_tree_fingerprint(self.root)
        self.assertIsInstance(fp1, tuple)

        # Fingerprint should be stable when nothing changes
        fp2 = compute_tree_fingerprint(self.root)
        self.assertEqual(fp1, fp2)

        # Touching or adding a file changes fingerprint
        (self.root / "file2.txt").write_text("two")
        fp3 = compute_tree_fingerprint(self.root)
        self.assertNotEqual(fp1, fp3)


class TestFuzzyAndRipgrep(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        (self.root / "src" / "utils").mkdir(parents=True)
        (self.root / "src" / "utils" / "helper.py").write_text("def find_answer():\n    return 42\n")
        (self.root / "README.md").write_text("# Project Docs\nWelcome to the answer project.\n")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "ignored.js").write_text("ignored")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_fuzzy_find_files(self):
        # Empty query lists files
        all_files = fuzzy_find_files(self.root, "")
        self.assertIn((self.root / "README.md").resolve(), all_files)
        self.assertIn((self.root / "src" / "utils" / "helper.py").resolve(), all_files)
        # Should ignore node_modules
        self.assertNotIn((self.root / "node_modules" / "ignored.js").resolve(), all_files)

        # Subsequence / fuzzy query
        matches = fuzzy_find_files(self.root, "suh")
        self.assertIn((self.root / "src" / "utils" / "helper.py").resolve(), matches)

    def test_ripgrep_search(self):
        matches = ripgrep_search(self.root, "find_answer")
        self.assertTrue(len(matches) >= 1)
        match = matches[0]
        self.assertEqual(match.path.resolve(), (self.root / "src" / "utils" / "helper.py").resolve())
        self.assertEqual(match.line, 1)
        self.assertIn("def find_answer", match.snippet)

        empty_matches = ripgrep_search(self.root, "non_existent_symbol_xyz")
        self.assertEqual(empty_matches, [])


class TestPreviewState(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        self.f1 = self.root / "file1.py"
        self.f1.write_text("x = 1\ny = 2\n")
        self.f2 = self.root / "doc.md"
        self.f2.write_text("# Header\nContent\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_tab_lifecycle(self):
        state = PreviewState()
        self.assertEqual(len(state.tabs), 0)
        self.assertIsNone(state.active_tab)

        # Open tab 1
        t1 = state.open_or_focus(self.f1)
        self.assertEqual(len(state.tabs), 1)
        self.assertEqual(state.active_index, 0)
        self.assertEqual(state.active_tab.path, self.f1.resolve())

        # Open tab 2
        t2 = state.open_or_focus(self.f2, line=2)
        self.assertEqual(len(state.tabs), 2)
        self.assertEqual(state.active_index, 1)
        self.assertEqual(state.active_tab.path, self.f2.resolve())
        self.assertEqual(state.active_tab.line, 2)

        # Re-focus tab 1 without duplicate
        state.open_or_focus(self.f1, line=1)
        self.assertEqual(len(state.tabs), 2)
        self.assertEqual(state.active_index, 0)
        self.assertEqual(state.active_tab.line, 1)

        # Navigation
        state.next_tab()
        self.assertEqual(state.active_index, 1)
        state.prev_tab()
        self.assertEqual(state.active_index, 0)

        # Toggle markdown
        state.next_tab()  # to doc.md
        self.assertFalse(state.active_tab.raw_markdown)
        state.toggle_markdown()
        self.assertTrue(state.active_tab.raw_markdown)

        # Close active tab
        state.close_active()
        self.assertEqual(len(state.tabs), 1)
        self.assertEqual(state.active_tab.path, self.f1.resolve())

    def test_render_tab(self):
        state = PreviewState()
        t1 = state.open_or_focus(self.f1, line=2)
        kind, renderable = state.render_tab(t1)
        self.assertEqual(kind, "syntax")

        t2 = state.open_or_focus(self.f2)
        kind, renderable = state.render_tab(t2)
        self.assertEqual(kind, "markdown")


class TestKittyController(unittest.TestCase):
    def setUp(self):
        self.controller = KittyController(socket="unix:/tmp/test-kitty-socket")

    def test_parse_windows(self):
        sample_ls = [
            {
                "id": 1,
                "is_active": True,
                "tabs": [
                    {
                        "id": 10,
                        "is_active": True,
                        "windows": [
                            {"id": 101, "is_active": True, "user_vars": {"role": "workspace"}},
                            {"id": 102, "is_active": False, "user_vars": {"role": "tree"}},
                        ],
                    }
                ],
            }
        ]
        info = self.controller.parse_windows(sample_ls)
        self.assertEqual(info["tab_id"], 10)
        self.assertEqual(info["roles"].get("workspace"), 101)
        self.assertEqual(info["roles"].get("tree"), 102)
        self.assertNotIn("preview", info["roles"])

    @patch("subprocess.run")
    def test_open_in_preview_spawns_when_no_preview_pane(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps([
            {
                "id": 1,
                "is_active": True,
                "tabs": [
                    {
                        "id": 12,
                        "is_active": True,
                        "windows": [
                            {"id": 101, "user_vars": {"role": "workspace"}},
                        ],
                    }
                ],
            }
        ]))
        test_file = Path("/tmp/test.py")
        ipc_file = self.controller.get_preview_ipc_path(tab_id=12)
        if ipc_file.exists():
            ipc_file.unlink()

        self.controller.open_in_preview(test_file, line=10)

        # Verify IPC state written
        self.assertTrue(ipc_file.exists())
        data = json.loads(ipc_file.read_text())
        self.assertEqual(data["path"], str(test_file.resolve()))
        self.assertEqual(data["line"], 10)

        # Verify kitty launch command called with role=preview
        calls = [call[0][0] for call in mock_run.call_args_list]
        launch_calls = [cmd for cmd in calls if "launch" in cmd]
        self.assertTrue(len(launch_calls) > 0)
        self.assertIn("--var=role=preview", " ".join(launch_calls[0]))

    @patch("subprocess.run")
    def test_open_in_editor(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps([
            {
                "id": 1,
                "is_active": True,
                "tabs": [
                    {
                        "id": 14,
                        "is_active": True,
                        "windows": [
                            {"id": 101, "user_vars": {"role": "workspace"}},
                        ],
                    }
                ],
            }
        ]))
        test_file = Path("/tmp/edit.py")
        self.controller.open_in_editor(test_file, line=5)

        calls = [call[0][0] for call in mock_run.call_args_list]
        launch_calls = [cmd for cmd in calls if "launch" in cmd]
        self.assertTrue(len(launch_calls) > 0)
        self.assertIn("role=editor", " ".join(launch_calls[0]))


class TestTextualApps(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        (self.root / "hello.py").write_text("print('hello world')\n")
        (self.root / "notes.md").write_text("# Notes\n* item 1\n* item 2\n")

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir)

    async def test_preview_app_pilot(self):
        app = KitcodePreviewApp(initial_files=[self.root / "hello.py", self.root / "notes.md"])
        async with app.run_test() as pilot:
            # Check widgets mounted
            self.assertIsNotNone(app.query_one("#tab-bar"))
            self.assertIsNotNone(app.query_one("#preview-body"))

            # Tab 1 should be active initially
            self.assertEqual(app.preview_state.active_index, 0)

            # Switch tab with '2' or ']'
            await pilot.press("2")
            self.assertEqual(app.preview_state.active_index, 1)

            # Toggle markdown
            await pilot.press("m")
            self.assertTrue(app.preview_state.active_tab.raw_markdown)

            # Test help modal opening and closing
            await pilot.press("question_mark")
            await pilot.press("escape")

            # Close tab with 'w'
            await pilot.press("w")
            self.assertEqual(len(app.preview_state.tabs), 1)

    async def test_tree_app_pilot(self):
        app = KitcodeTreeApp(root_dir=self.root)
        async with app.run_test() as pilot:
            # Check widgets mounted
            self.assertIsNotNone(app.query_one("#header-bar"))
            self.assertIsNotNone(app.query_one("#tree-view"))

            # Press '/' to open fuzzy finder
            await pilot.press("slash")
            search_input = app.query_one("#search-input")
            self.assertTrue(search_input.display)

            # Press Escape to close search
            await pilot.press("escape")
            self.assertFalse(search_input.display)

            # Press '?' to open help modal and escape to close
            await pilot.press("question_mark")
            await pilot.press("escape")


class TestCliAndBinaries(unittest.TestCase):
    def setUp(self):
        self.bin_dir = REPO_ROOT / "bin"
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        self.sample_md = self.root / "sample.md"
        self.sample_md.write_text("# Hello Kitcode\nThis is a test document.\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_preview_dump_flag(self):
        cmd = [sys.executable, str(self.bin_dir / "kitcode-preview"), "--dump", str(self.sample_md)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("Hello Kitcode", res.stdout)
        self.assertIn("This is a test document", res.stdout)

    def test_toggle_cli_invocations(self):
        cmd = [sys.executable, str(self.bin_dir / "kitcode-toggle"), "tree"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        # Without a running kitty socket it should exit cleanly (returncode 0) without unhandled tracebacks
        self.assertEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
