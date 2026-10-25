"""
kitcode_core: Pure-Python and Textual core components for Kitcode Kitty-native IDE.
Handles:
- Git status parsing & badges
- Fast filesystem fingerprinting
- Fuzzy file search & ripgrep search
- Multi-tab preview state & rich rendering
- Agent-first Kitty IPC routing & pane management
- Upgraded KitcodeTreeApp and KitcodePreviewApp
"""

import json
import os
import re
import shlex
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rich.markdown import Markdown
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import DirectoryTree, Input, Label, OptionList, Static
from textual.widgets._directory_tree import DirEntry
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode


# ==============================================================================
# Git Status & Filesystem
# ==============================================================================

@dataclass
class GitRepoState:
    branch: str | None = None
    file_badges: dict[Path, str] = field(default_factory=dict)
    dirty_dirs: set[Path] = field(default_factory=set)

    @property
    def dirty_count(self) -> int:
        return len(self.file_badges)


def get_git_status(root_dir: Path) -> GitRepoState:
    """Run git status and return structured repository state with badges and dirty dirs."""
    root_dir = root_dir.resolve()
    try:
        res = subprocess.run(
            ["git", "-C", str(root_dir), "status", "--porcelain=v1", "-b", "-uall"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if res.returncode != 0:
            return GitRepoState()
    except Exception:
        return GitRepoState()

    lines = res.stdout.splitlines()
    if not lines:
        return GitRepoState()

    branch = None
    file_badges: dict[Path, str] = {}
    dirty_dirs: set[Path] = set()

    # Parse branch from header (## ...)
    header = lines[0].strip()
    if header.startswith("##"):
        branch_part = header[2:].strip()
        if branch_part.startswith("No commits yet on "):
            branch = branch_part[len("No commits yet on "):].split()[0]
        elif branch_part.startswith("Initial commit on "):
            branch = branch_part[len("Initial commit on "):].split()[0]
        elif branch_part.startswith("HEAD"):
            branch = "HEAD"
        else:
            branch = branch_part.split("...")[0].split()[0]

    # Parse file statuses
    for line in lines[1:]:
        if len(line) < 4:
            continue
        xy = line[:2]
        path_str = line[3:].strip()
        if " -> " in path_str:
            path_str = path_str.split(" -> ")[1].strip()
        if path_str.startswith('"') and path_str.endswith('"'):
            path_str = path_str[1:-1]

        x, y = xy[0], xy[1]
        if x == "?" or y == "?":
            badge = "U"
        elif x == "A" or y == "A":
            badge = "A"
        elif x == "M" or y == "M":
            badge = "M"
        elif x == "D" or y == "D":
            badge = "D"
        elif x == "R" or y == "R":
            badge = "R"
        else:
            badge = "M"

        abs_file = (root_dir / path_str).resolve()
        file_badges[abs_file] = badge

        # Mark parent directories as dirty
        curr = abs_file.parent
        while curr != root_dir and root_dir in curr.parents:
            dirty_dirs.add(curr)
            curr = curr.parent
        if curr == root_dir:
            dirty_dirs.add(root_dir)

    return GitRepoState(branch=branch, file_badges=file_badges, dirty_dirs=dirty_dirs)


def compute_tree_fingerprint(root_dir: Path) -> tuple:
    """Fast stat check on root, .git/HEAD, .git/index, and immediate entries."""
    root_dir = root_dir.resolve()
    mtimes = []
    try:
        mtimes.append(root_dir.stat().st_mtime_ns)
    except OSError:
        mtimes.append(0)

    git_dir = root_dir / ".git"
    for ref in (git_dir / "HEAD", git_dir / "index"):
        try:
            mtimes.append(ref.stat().st_mtime_ns)
        except OSError:
            mtimes.append(0)

    try:
        entries = []
        with os.scandir(root_dir) as it:
            for entry in it:
                if entry.name != ".git":
                    try:
                        entries.append((entry.name, entry.stat().st_mtime_ns))
                    except OSError:
                        pass
        entries.sort()
        mtimes.append(tuple(entries))
    except OSError:
        pass

    return tuple(mtimes)


# ==============================================================================
# Search: Fuzzy Find & Ripgrep
# ==============================================================================

def fuzzy_find_files(
    root_dir: Path, query: str, show_hidden: bool = False, limit: int = 100
) -> list[Path]:
    """Find files in project matching query using subsequence/substring matching."""
    root_dir = root_dir.resolve()
    paths: list[str] = []

    # Attempt rg --files first
    cmd = ["rg", "--files", "--color=never"]
    if show_hidden:
        cmd.append("--hidden")
    cmd.extend([
        "--glob", "!.git/*",
        "--glob", "!node_modules/*",
        "--glob", "!__pycache__/*",
        "--glob", "!.next/*",
        "--glob", "!target/*",
        "--glob", "!dist/*",
    ])

    try:
        res = subprocess.run(cmd, cwd=root_dir, capture_output=True, text=True, timeout=2.0, check=False)
        if res.returncode == 0:
            paths = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        pass

    # Fallback to os.walk if rg was empty or failed
    if not paths:
        ignored = {".git", "node_modules", "__pycache__", ".next", "target", "dist"}
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if d not in ignored and (show_hidden or not d.startswith("."))]
            for f in files:
                if not show_hidden and f.startswith("."):
                    continue
                full = Path(root) / f
                try:
                    rel = full.relative_to(root_dir)
                    paths.append(str(rel))
                except ValueError:
                    paths.append(str(full))

    if not query.strip():
        return [(root_dir / p).resolve() for p in paths[:limit]]

    q = query.lower()
    scored: list[tuple[int, str]] = []

    for p in paths:
        p_lower = p.lower()
        if q in p_lower:
            # Substring match (high score)
            score = 1000 - len(p) + (500 if p_lower.endswith(q) else 0)
            scored.append((score, p))
            continue

        # Subsequence match
        p_idx = 0
        q_idx = 0
        while p_idx < len(p_lower) and q_idx < len(q):
            if p_lower[p_idx] == q[q_idx]:
                q_idx += 1
            p_idx += 1
        if q_idx == len(q):
            score = 100 - len(p)
            scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [(root_dir / p).resolve() for _, p in scored[:limit]]


@dataclass
class SearchMatch:
    path: Path
    line: int
    col: int
    snippet: str


def ripgrep_search(root_dir: Path, query: str, limit: int = 100) -> list[SearchMatch]:
    """Search file contents using ripgrep --vimgrep."""
    if not query.strip():
        return []

    root_dir = root_dir.resolve()
    cmd = [
        "rg",
        "--vimgrep",
        "--color=never",
        "--smart-case",
        "--max-count=50",
        "--glob", "!.git/*",
        "--glob", "!node_modules/*",
        "--glob", "!target/*",
        "--glob", "!dist/*",
        query,
        str(root_dir),
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0, check=False)
        lines = res.stdout.splitlines()
    except Exception:
        return []

    matches: list[SearchMatch] = []
    for line in lines[:limit]:
        parts = line.split(":", 3)
        if len(parts) >= 4:
            file_str, line_str, col_str, snippet = parts[0], parts[1], parts[2], parts[3]
            try:
                matches.append(
                    SearchMatch(
                        path=Path(file_str).resolve(),
                        line=int(line_str),
                        col=int(col_str),
                        snippet=snippet.strip(),
                    )
                )
            except ValueError:
                continue

    return matches


# ==============================================================================
# Preview State & Content Rendering
# ==============================================================================

@dataclass
class PreviewTab:
    path: Path
    line: int | None = None
    raw_markdown: bool = False


class PreviewState:
    """Manages open tabs and content rendering in kitcode-preview."""

    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
    MARKDOWN_EXTENSIONS = {".md", ".markdown"}

    def __init__(self):
        self.tabs: list[PreviewTab] = []
        self.active_index: int = 0

    @property
    def active_tab(self) -> PreviewTab | None:
        if 0 <= self.active_index < len(self.tabs):
            return self.tabs[self.active_index]
        return None

    def open_or_focus(self, path: Path, line: int | None = None) -> PreviewTab:
        resolved = path.resolve()
        for idx, tab in enumerate(self.tabs):
            if tab.path == resolved:
                self.active_index = idx
                if line is not None:
                    tab.line = line
                return tab

        tab = PreviewTab(path=resolved, line=line)
        self.tabs.append(tab)
        self.active_index = len(self.tabs) - 1
        return tab

    def close_active(self) -> None:
        if self.tabs:
            self.tabs.pop(self.active_index)
            if self.active_index >= len(self.tabs):
                self.active_index = max(0, len(self.tabs) - 1)

    def select_index(self, idx: int) -> None:
        if 0 <= idx < len(self.tabs):
            self.active_index = idx

    def next_tab(self) -> None:
        if self.tabs:
            self.active_index = (self.active_index + 1) % len(self.tabs)

    def prev_tab(self) -> None:
        if self.tabs:
            self.active_index = (self.active_index - 1) % len(self.tabs)

    def toggle_markdown(self) -> None:
        tab = self.active_tab
        if tab and tab.path.suffix.lower() in self.MARKDOWN_EXTENSIONS:
            tab.raw_markdown = not tab.raw_markdown

    def _get_image_metadata(self, path: Path) -> str:
        """Extract image dimensions and file size using pure Python stdlib."""
        try:
            size_bytes = path.stat().st_size
            size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024*1024):.2f} MB"
            ext = path.suffix.lower()
            dim_str = ""

            with open(path, "rb") as f:
                header = f.read(32)
                if ext == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                    w, h = struct.unpack(">II", header[16:24])
                    dim_str = f"{w}x{h}"
                elif ext == ".gif" and header.startswith((b"GIF87a", b"GIF89a")) and len(header) >= 10:
                    w, h = struct.unpack("<HH", header[6:10])
                    dim_str = f"{w}x{h}"

            dims = f" ({dim_str})" if dim_str else ""
            return f"🖼️ Image: {path.name}{dims} — {size_str}\n\nPress [i] to preview in Kitty terminal or open externally."
        except Exception as e:
            return f"🖼️ Image: {path.name} (unable to parse metadata: {e})"

    def render_tab(self, tab: PreviewTab) -> tuple[str, Any]:
        """Render tab content returning (kind, renderable)."""
        if not tab.path.exists():
            return "text", Text(f"File not found: {tab.path}", style="bold red")

        ext = tab.path.suffix.lower()

        # Image preview
        if ext in self.IMAGE_EXTENSIONS:
            meta = self._get_image_metadata(tab.path)
            return "image", meta

        # Formatted Markdown
        if ext in self.MARKDOWN_EXTENSIONS and not tab.raw_markdown:
            try:
                content = tab.path.read_text(encoding="utf-8", errors="replace")
                return "markdown", Markdown(content)
            except Exception as e:
                return "text", Text(f"Error reading markdown: {e}", style="red")

        # Code / Text / Syntax
        try:
            raw = tab.path.read_bytes()
            if b"\x00" in raw[:1024]:
                return "text", Text(f"[Binary file: {len(raw)} bytes]\n{tab.path}", style="#6c7086")

            highlight = {tab.line} if tab.line else set()
            syntax = Syntax.from_path(
                str(tab.path),
                line_numbers=True,
                theme="monokai",
                highlight_lines=highlight,
            )
            return "syntax", syntax
        except Exception:
            try:
                content = tab.path.read_text(encoding="utf-8", errors="replace")
                return "text", Text(content)
            except Exception as e:
                return "text", Text(f"Error reading file: {e}", style="red")


# ==============================================================================
# Agent-First Kitty Routing & IPC
# ==============================================================================

class KittyController:
    """Controls Kitty windows via Kitty remote control socket without polluting workspace."""

    def __init__(self, socket: str | None = None):
        self.socket = socket or os.environ.get("KITTY_LISTEN_ON", "unix:/tmp/kitty-socket")

    def run_kitty(self, *args: str) -> subprocess.CompletedProcess:
        """Run a kitty remote control command."""
        cmd = ["kitty", "@", "--to", self.socket, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2.0, check=False)

    def get_windows(self) -> list[dict]:
        """Retrieve kitty window hierarchy."""
        try:
            res = self.run_kitty("ls")
            if res.returncode == 0:
                return json.loads(res.stdout)
        except Exception:
            pass
        return []

    def parse_windows(self, ls_data: list[dict]) -> dict[str, Any]:
        """Find active tab, active window, and role mappings."""
        result: dict[str, Any] = {
            "tab_id": 1,
            "roles": {},
            "active_window_id": None,
        }
        for os_win in ls_data:
            if not os_win.get("is_active", True) and len(ls_data) > 1:
                continue
            for tab in os_win.get("tabs", []):
                if tab.get("is_active", True) or len(os_win.get("tabs", [])) == 1:
                    result["tab_id"] = tab.get("id", 1)
                    for win in tab.get("windows", []):
                        wid = win.get("id")
                        if win.get("is_active"):
                            result["active_window_id"] = wid
                        user_vars = win.get("user_vars", {})
                        role = user_vars.get("role")
                        if role:
                            result["roles"][role] = wid
                    return result
        return result

    def get_preview_ipc_path(self, tab_id: int | str = "default") -> Path:
        return Path(f"/tmp/kitcode-preview-{tab_id}.json")

    def open_in_preview(
        self, file_path: Path, line: int | None = None, root_dir: Path | None = None
    ) -> None:
        """Open or update file in dedicated preview pane without touching workspace."""
        ls_info = self.parse_windows(self.get_windows())
        tab_id = ls_info["tab_id"]
        ipc_file = self.get_preview_ipc_path(tab_id)

        # Write IPC state file for kitcode-preview to consume
        state_data = {
            "action": "open",
            "path": str(file_path.resolve()),
            "line": line,
            "timestamp": time.time(),
        }
        try:
            ipc_file.write_text(json.dumps(state_data))
        except Exception:
            pass

        has_preview = "preview" in ls_info["roles"]
        if not has_preview:
            # Spawn preview split next to workspace
            cwd = str(root_dir.resolve()) if root_dir else str(file_path.parent)
            args = [
                "launch",
                "--location=vsplit",
                "--bias=45",
                "--var=role=preview",
                "--title=Preview",
                f"--cwd={cwd}",
                "kitcode-preview",
                f"--tab-id={tab_id}",
                str(file_path.resolve()),
            ]
            if line:
                args.append(f"+{line}")
            self.run_kitty(*args)
        else:
            self.run_kitty("focus-window", "--match", "var:role=preview")

    def open_in_editor(
        self, file_path: Path, line: int | None = None, root_dir: Path | None = None
    ) -> None:
        """Open file in $EDITOR in dedicated editor pane without touching workspace."""
        ls_info = self.parse_windows(self.get_windows())
        tab_id = ls_info["tab_id"]
        editor = os.environ.get("EDITOR", "nvim")
        editor_sock = Path(f"/tmp/kitcode-nvim-{tab_id}.sock")
        line_arg = f"+{line}" if line else "+1"

        if "editor" in ls_info["roles"] and editor_sock.exists():
            subprocess.run(
                ["nvim", "--server", str(editor_sock), "--remote-tab", line_arg, str(file_path.resolve())],
                check=False,
            )
            self.run_kitty("focus-window", "--match", "var:role=editor")
        else:
            cwd = str(root_dir.resolve()) if root_dir else str(file_path.parent)
            cmd = [
                "launch",
                "--location=vsplit",
                "--bias=50",
                "--var=role=editor",
                "--title=Editor",
                f"--cwd={cwd}",
                editor,
            ]
            if "nvim" in editor:
                cmd.extend(["--listen", str(editor_sock)])
            if line:
                cmd.append(f"+{line}")
            cmd.append(str(file_path.resolve()))
            self.run_kitty(*cmd)
            self.run_kitty("focus-window", "--match", "var:role=editor")

    def toggle_pane(self, role: str, root_dir: Path | None = None) -> None:
        """Toggle tree or preview pane on/off."""
        ls_info = self.parse_windows(self.get_windows())
        if role in ls_info["roles"]:
            self.run_kitty("close-window", "--match", f"var:role={role}")
        else:
            cwd = str(root_dir.resolve()) if root_dir else "."
            if role == "tree":
                self.run_kitty(
                    "launch",
                    "--location=vsplit",
                    "--bias=22",
                    "--var=role=tree",
                    "--title=FileTree",
                    f"--cwd={cwd}",
                    "kitcode-tree",
                )
            elif role == "preview":
                self.run_kitty(
                    "launch",
                    "--location=vsplit",
                    "--bias=45",
                    "--var=role=preview",
                    "--title=Preview",
                    f"--cwd={cwd}",
                    "kitcode-preview",
                )


# ==============================================================================
# Textual App: KitcodePreviewApp
# ==============================================================================

class KitcodePreviewApp(App):
    """Multi-tab syntax, markdown, and media preview viewer for Kitcode."""

    CSS = """
    Screen {
        background: #1e1e2e;
        color: #cdd6f4;
        overflow: hidden;
    }

    #tab-bar {
        dock: top;
        height: 1;
        background: #181825;
        color: #6c7086;
        padding: 0 1;
    }

    #preview-body {
        width: 100%;
        height: 1fr;
        background: #1e1e2e;
        color: #cdd6f4;
        overflow-y: scroll;
        padding: 0 1;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: #181825;
        color: #6c7086;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Close Pane", show=False),
        Binding("w", "close_tab", "Close Tab", show=True),
        Binding("m", "toggle_markdown", "Toggle Markdown", show=True),
        Binding("e", "edit_in_editor", "Edit", show=True),
        Binding("i", "show_image", "Image", show=True),
        Binding("bracket_left", "prev_tab", "Prev Tab", show=False),
        Binding("bracket_right", "next_tab", "Next Tab", show=False),
        Binding("1", "tab_1", "Tab 1", show=False),
        Binding("2", "tab_2", "Tab 2", show=False),
        Binding("3", "tab_3", "Tab 3", show=False),
        Binding("4", "tab_4", "Tab 4", show=False),
        Binding("5", "tab_5", "Tab 5", show=False),
        Binding("6", "tab_6", "Tab 6", show=False),
        Binding("7", "tab_7", "Tab 7", show=False),
        Binding("8", "tab_8", "Tab 8", show=False),
        Binding("9", "tab_9", "Tab 9", show=False),
    ]

    def __init__(
        self,
        initial_files: list[Path | str] | None = None,
        tab_id: int | str = "default",
        target_line: int | None = None,
    ):
        super().__init__()
        self.tab_id = tab_id
        self.preview_state = PreviewState()
        self.last_ipc_ts = 0.0

        if initial_files:
            for idx, f in enumerate(initial_files):
                p = Path(f)
                line = target_line if idx == 0 else None
                self.preview_state.open_or_focus(p, line=line)
            self.preview_state.select_index(0)

    def compose(self) -> ComposeResult:
        yield Label("", id="tab-bar")
        yield Static("", id="preview-body")
        yield Label(
            Text("[1-9 / [ / ]] tab  [e] edit  [m] md raw/render  [i] image  [w] close  [q] quit"),
            id="status-bar",
        )

    def on_mount(self) -> None:
        self.update_view()
        self.set_interval(0.15, self._check_ipc)

    def _check_ipc(self) -> None:
        ipc_file = Path(f"/tmp/kitcode-preview-{self.tab_id}.json")
        if not ipc_file.exists():
            return
        try:
            data = json.loads(ipc_file.read_text())
            ts = data.get("timestamp", 0)
            if ts > self.last_ipc_ts:
                self.last_ipc_ts = ts
                path = Path(data.get("path"))
                line = data.get("line")
                self.preview_state.open_or_focus(path, line=line)
                self.update_view()
        except Exception:
            pass

    def update_view(self) -> None:
        tab_bar = self.query_one("#tab-bar", Label)
        body = self.query_one("#preview-body", Static)

        # Render tabs
        tab_texts = []
        for idx, tab in enumerate(self.preview_state.tabs):
            num = idx + 1
            name = tab.path.name
            if idx == self.preview_state.active_index:
                tab_texts.append(Text(f" {num}: {name} ", style="bold #1e1e2e on #89b4fa"))
            else:
                tab_texts.append(Text(f" {num}: {name} ", style="#a6adc8"))

        if tab_texts:
            res = Text(" ").join(tab_texts)
            tab_bar.update(res)
        else:
            tab_bar.update(Text(" No open files ", style="#6c7086"))

        # Render active tab
        active = self.preview_state.active_tab
        if active:
            kind, renderable = self.preview_state.render_tab(active)
            body.update(renderable)
            if active.line and kind == "syntax":
                # Scroll near target line
                body.scroll_to(y=max(0, active.line - 5), animate=False)
        else:
            body.update(Text("No buffer open. Select a file in kitcode-tree.", style="#6c7086"))

    def action_close_tab(self) -> None:
        self.preview_state.close_active()
        if not self.preview_state.tabs:
            self.exit()
        else:
            self.update_view()

    def action_toggle_markdown(self) -> None:
        self.preview_state.toggle_markdown()
        self.update_view()

    def action_prev_tab(self) -> None:
        self.preview_state.prev_tab()
        self.update_view()

    def action_next_tab(self) -> None:
        self.preview_state.next_tab()
        self.update_view()

    def action_edit_in_editor(self) -> None:
        tab = self.preview_state.active_tab
        if tab:
            controller = KittyController()
            controller.open_in_editor(tab.path, line=tab.line)

    def action_show_image(self) -> None:
        tab = self.preview_state.active_tab
        if tab and tab.path.suffix.lower() in self.preview_state.IMAGE_EXTENSIONS:
            try:
                subprocess.run(["kitty", "+kitten", "icat", str(tab.path)], check=False)
            except Exception:
                subprocess.run(["xdg-open", str(tab.path)], check=False)

    def action_tab_1(self) -> None: self._switch_tab(0)
    def action_tab_2(self) -> None: self._switch_tab(1)
    def action_tab_3(self) -> None: self._switch_tab(2)
    def action_tab_4(self) -> None: self._switch_tab(3)
    def action_tab_5(self) -> None: self._switch_tab(4)
    def action_tab_6(self) -> None: self._switch_tab(5)
    def action_tab_7(self) -> None: self._switch_tab(6)
    def action_tab_8(self) -> None: self._switch_tab(7)
    def action_tab_9(self) -> None: self._switch_tab(8)

    def _switch_tab(self, idx: int) -> None:
        self.preview_state.select_index(idx)
        self.update_view()


# ==============================================================================
# Textual App: KitcodeTreeApp
# ==============================================================================

# Nerd Font Icons with Catppuccin Mocha Colors
FOLDER_CLOSED = ("󰉋 ", "#89b4fa")
FOLDER_OPEN   = ("󰝰 ", "#89dceb")
DEFAULT_FILE  = ("󰈔 ", "#a6adc8")

EXACT_MAP = {
    "dockerfile":         ("󰡨 ", "#89b4fa"),
    "docker-compose.yml": ("󰡨 ", "#89b4fa"),
    "docker-compose.yaml":("󰡨 ", "#89b4fa"),
    "makefile":           (" ", "#f38ba8"),
    "cmakelists.txt":     (" ", "#f38ba8"),
    "package.json":       (" ", "#a6e3a1"),
    "package-lock.json":  (" ", "#6c7086"),
    "cargo.toml":         (" ", "#fab387"),
    "cargo.lock":         (" ", "#6c7086"),
    "license":            (" ", "#f9e2af"),
    "licence":            (" ", "#f9e2af"),
    "readme.md":          (" ", "#89b4fa"),
    ".gitignore":         (" ", "#f38ba8"),
    ".gitmodules":        (" ", "#f38ba8"),
    ".env":               (" ", "#f9e2af"),
    ".env.local":         (" ", "#f9e2af"),
}

ICON_MAP = {
    ".py":    (" ", "#f9e2af"),
    ".pyc":   (" ", "#6c7086"),
    ".rs":    (" ", "#fab387"),
    ".go":    (" ", "#94e2d5"),
    ".js":    (" ", "#f9e2af"),
    ".mjs":   (" ", "#f9e2af"),
    ".cjs":   (" ", "#f9e2af"),
    ".jsx":   (" ", "#89dceb"),
    ".ts":    (" ", "#89b4fa"),
    ".tsx":   (" ", "#89dceb"),
    ".html":  (" ", "#fab387"),
    ".htm":   (" ", "#fab387"),
    ".css":   (" ", "#89b4fa"),
    ".scss":  (" ", "#f5c2e7"),
    ".sass":  (" ", "#f5c2e7"),
    ".less":  (" ", "#89b4fa"),
    ".json":  (" ", "#f9e2af"),
    ".jsonc": (" ", "#f9e2af"),
    ".yaml":  (" ", "#cba6f7"),
    ".yml":   (" ", "#cba6f7"),
    ".toml":  (" ", "#b4befe"),
    ".md":    (" ", "#89b4fa"),
    ".markdown": (" ", "#89b4fa"),
    ".sh":    (" ", "#a6e3a1"),
    ".zsh":   (" ", "#a6e3a1"),
    ".bash":  (" ", "#a6e3a1"),
    ".fish":  (" ", "#94e2d5"),
    ".c":     (" ", "#89b4fa"),
    ".cpp":   (" ", "#89b4fa"),
    ".cc":    (" ", "#89b4fa"),
    ".h":     (" ", "#cba6f7"),
    ".hpp":   (" ", "#cba6f7"),
    ".lua":   (" ", "#89b4fa"),
    ".sql":   (" ", "#fab387"),
    ".vue":   ("󰡄 ", "#a6e3a1"),
    ".svelte":(" ", "#fab387"),
    ".java":  (" ", "#fab387"),
    ".kt":    (" ", "#cba6f7"),
    ".dart":  (" ", "#89dceb"),
    ".php":   (" ", "#cba6f7"),
    ".rb":    (" ", "#f38ba8"),
    ".swift": (" ", "#fab387"),
    ".zig":   (" ", "#fab387"),
    ".conf":  (" ", "#b4befe"),
    ".config":(" ", "#b4befe"),
    ".ini":   (" ", "#b4befe"),
    ".lock":  (" ", "#f38ba8"),
    ".csv":   (" ", "#a6e3a1"),
    ".tsv":   (" ", "#a6e3a1"),
    ".xml":   ("󰗀 ", "#fab387"),
    ".log":   (" ", "#6c7086"),
    ".txt":   (" ", "#cdd6f4"),
    ".pdf":   (" ", "#f38ba8"),
    ".png":   (" ", "#cba6f7"),
    ".jpg":   (" ", "#cba6f7"),
    ".jpeg":  (" ", "#cba6f7"),
    ".gif":   (" ", "#cba6f7"),
    ".webp":  (" ", "#cba6f7"),
    ".svg":   ("󰜡 ", "#f9e2af"),
    ".mp3":   (" ", "#f5c2e7"),
    ".wav":   (" ", "#f5c2e7"),
    ".mp4":   (" ", "#89dceb"),
    ".mkv":   (" ", "#89dceb"),
    ".zip":   (" ", "#fab387"),
    ".tar":   (" ", "#fab387"),
    ".gz":    (" ", "#fab387"),
    ".7z":    (" ", "#fab387"),
}


class FileTree(DirectoryTree):
    """Custom DirectoryTree with Material/Nerd Font icons and Git status badges."""

    def __init__(self, path: str | Path, git_state_getter: Callable[[], GitRepoState], show_hidden: bool = False, **kwargs):
        super().__init__(path, **kwargs)
        self.git_state_getter = git_state_getter
        self.show_hidden = show_hidden
        self.show_root = False

    def filter_paths(self, paths):
        if self.show_hidden:
            return sorted(paths, key=lambda p: (not p.is_dir(), p.name.lower()))
        return sorted(
            [p for p in paths if not p.name.startswith(".")],
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )

    def render_label(self, node: TreeNode[DirEntry], base_style: Style, style: Style) -> Text:
        node_label = node._label.copy()
        node_label.stylize(style)

        if not self.is_mounted:
            return node_label

        name = node_label.plain
        name_lower = name.lower()
        git_state = self.git_state_getter()
        node_path = node.data.path.resolve() if node.data and hasattr(node.data, "path") else None

        badge_text = Text()

        if node._allow_expand:
            icon_str, icon_color = FOLDER_OPEN if node.is_expanded else FOLDER_CLOSED
            prefix = Text(icon_str, style=icon_color)
            node_label.stylize_before("bold #cdd6f4")
            if node_path and node_path in git_state.dirty_dirs:
                badge_text = Text(" ●", style="#f9e2af")
        else:
            if name_lower in EXACT_MAP:
                icon_str, icon_color = EXACT_MAP[name_lower]
            else:
                ext = Path(name_lower).suffix
                icon_str, icon_color = ICON_MAP.get(ext, DEFAULT_FILE)

            prefix = Text(icon_str, style=icon_color)
            if name.startswith("."):
                node_label.stylize_before("#6c7086")
            else:
                node_label.stylize_before("#cdd6f4")

            if node_path and node_path in git_state.file_badges:
                b = git_state.file_badges[node_path]
                colors = {"M": "#f9e2af", "A": "#a6e3a1", "U": "#fab387", "D": "#f38ba8", "R": "#cba6f7"}
                badge_text = Text(f" [{b}]", style=colors.get(b, "#f9e2af"))

        return Text.assemble(prefix, node_label, badge_text)


class KitcodeTreeApp(App):
    """Textual tree explorer for Kitcode with Git badges, search & preview routing."""

    CSS = """
    Screen {
        background: #1e1e2e;
        color: #cdd6f4;
        overflow: hidden;
    }

    #header-bar {
        dock: top;
        height: 1;
        background: #181825;
        color: #89b4fa;
        text-style: bold;
        padding: 0 1;
    }

    #search-box {
        dock: top;
        height: auto;
        background: #181825;
        padding: 0 1;
    }

    #search-input {
        background: #313244;
        color: #cdd6f4;
        border: none;
        height: 1;
    }

    #search-results {
        max-height: 8;
        background: #181825;
        color: #cdd6f4;
        border: none;
    }

    #tree-view {
        background: #1e1e2e;
        color: #cdd6f4;
        scrollbar-gutter: stable;
        border: none;
    }

    #tree-view:focus {
        border: none;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: #181825;
        color: #6c7086;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False),
        Binding("h", "toggle_hidden", "Toggle Hidden", show=True),
        Binding(".", "toggle_hidden", "Toggle Hidden", show=False),
        Binding("r", "refresh_tree", "Refresh", show=True),
        Binding("e", "edit_file", "Edit ($EDITOR)", show=True),
        Binding("b", "preview_file", "Preview", show=True),
        Binding("enter", "preview_file", "Preview", show=True),
        Binding("slash", "open_fuzzy_search", "Find File (/)", show=True),
        Binding("g", "open_ripgrep_search", "Ripgrep (g)", show=True),
        Binding("escape", "close_search", "Close Search", show=False),
    ]

    def __init__(self, root_dir: str | Path = "."):
        super().__init__()
        self.root_dir = Path(root_dir).resolve()
        self.show_hidden = False
        self.kitty_controller = KittyController()
        self.git_state = get_git_status(self.root_dir)
        self.fingerprint = compute_tree_fingerprint(self.root_dir)

        self.search_mode: str | None = None  # "fuzzy" or "ripgrep"
        self.search_matches: list[Any] = []

        self.file_tree = FileTree(
            self.root_dir,
            git_state_getter=lambda: self.git_state,
            show_hidden=self.show_hidden,
            id="tree-view",
        )

    def compose(self) -> ComposeResult:
        dir_name = self.root_dir.name or str(self.root_dir)
        branch_str = f"  󰊢 {self.git_state.branch}" if self.git_state.branch else ""
        dirty_str = f" [+{self.git_state.dirty_count}]" if self.git_state.dirty_count > 0 else ""
        yield Label(f"󰉋 {dir_name}{branch_str}{dirty_str}", id="header-bar")

        with Container(id="search-box"):
            search_input = Input(placeholder="Search...", id="search-input")
            search_input.display = False
            yield search_input
            results = OptionList(id="search-results")
            results.display = False
            yield results

        yield self.file_tree
        yield Label(Text("[Click/Enter] preview  [/] find  [g] grep  [e] edit  [r] reload"), id="status-bar")

    def on_mount(self) -> None:
        self.set_interval(2.0, self._check_fingerprint)

    def _check_fingerprint(self) -> None:
        new_fp = compute_tree_fingerprint(self.root_dir)
        if new_fp != self.fingerprint:
            self.fingerprint = new_fp
            self.refresh_all()

    def refresh_all(self) -> None:
        self.git_state = get_git_status(self.root_dir)
        dir_name = self.root_dir.name or str(self.root_dir)
        branch_str = f"  󰊢 {self.git_state.branch}" if self.git_state.branch else ""
        dirty_str = f" [+{self.git_state.dirty_count}]" if self.git_state.dirty_count > 0 else ""
        self.query_one("#header-bar", Label).update(f"󰉋 {dir_name}{branch_str}{dirty_str}")
        self.file_tree.reload()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.kitty_controller.open_in_preview(event.path, root_dir=self.root_dir)

    def action_preview_file(self) -> None:
        node = self.file_tree.cursor_node
        if node and node.data and isinstance(node.data.path, Path) and node.data.path.is_file():
            self.kitty_controller.open_in_preview(node.data.path, root_dir=self.root_dir)

    def action_edit_file(self) -> None:
        node = self.file_tree.cursor_node
        if node and node.data and isinstance(node.data.path, Path) and node.data.path.is_file():
            self.kitty_controller.open_in_editor(node.data.path, root_dir=self.root_dir)

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self.file_tree.show_hidden = self.show_hidden
        self.file_tree.reload()

    def action_refresh_tree(self) -> None:
        self.refresh_all()

    # Search actions
    def action_open_fuzzy_search(self) -> None:
        self.search_mode = "fuzzy"
        inp = self.query_one("#search-input", Input)
        results = self.query_one("#search-results", OptionList)
        inp.placeholder = "Fuzzy file search..."
        inp.value = ""
        inp.display = True
        results.display = True
        inp.focus()
        self._update_search_results("")

    def action_open_ripgrep_search(self) -> None:
        self.search_mode = "ripgrep"
        inp = self.query_one("#search-input", Input)
        results = self.query_one("#search-results", OptionList)
        inp.placeholder = "Ripgrep content search..."
        inp.value = ""
        inp.display = True
        results.display = True
        inp.focus()
        self._update_search_results("")

    def action_close_search(self) -> None:
        self.search_mode = None
        inp = self.query_one("#search-input", Input)
        results = self.query_one("#search-results", OptionList)
        inp.display = False
        results.display = False
        self.file_tree.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.search_mode:
            self._update_search_results(event.value)

    def _update_search_results(self, query: str) -> None:
        results = self.query_one("#search-results", OptionList)
        results.clear_options()

        if self.search_mode == "fuzzy":
            files = fuzzy_find_files(self.root_dir, query, show_hidden=self.show_hidden, limit=30)
            self.search_matches = files
            for f in files:
                try:
                    rel = f.relative_to(self.root_dir)
                    label = str(rel)
                except ValueError:
                    label = str(f)
                results.add_option(Option(label))
        elif self.search_mode == "ripgrep":
            matches = ripgrep_search(self.root_dir, query, limit=30)
            self.search_matches = matches
            for m in matches:
                try:
                    rel = m.path.relative_to(self.root_dir)
                    file_str = str(rel)
                except ValueError:
                    file_str = str(m.path)
                label = f"{file_str}:{m.line} — {m.snippet}"
                results.add_option(Option(label))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self.search_matches):
            item = self.search_matches[idx]
            if isinstance(item, Path):
                self.kitty_controller.open_in_preview(item, root_dir=self.root_dir)
            elif isinstance(item, SearchMatch):
                self.kitty_controller.open_in_preview(item.path, line=item.line, root_dir=self.root_dir)
            self.action_close_search()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        results = self.query_one("#search-results", OptionList)
        if self.search_matches:
            idx = results.highlighted if results.highlighted is not None else 0
            if 0 <= idx < len(self.search_matches):
                item = self.search_matches[idx]
                if isinstance(item, Path):
                    self.kitty_controller.open_in_preview(item, root_dir=self.root_dir)
                elif isinstance(item, SearchMatch):
                    self.kitty_controller.open_in_preview(item.path, line=item.line, root_dir=self.root_dir)
                self.action_close_search()
