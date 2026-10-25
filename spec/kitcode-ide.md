# Kitcode IDE — Specification

## Problem Statement
`kitcode` opens a 2-pane Kitty session (left shell workspace + right `kitcode-tree` sidebar) and uses `kitty @ send-text` to inject `cat`, `bat`, or `nvim` into the left workspace pane. When an AI coding agent (`claude`, `codex`, `agy`) or server runs in the workspace pane, clicking a file injects raw shell commands into the active process. Furthermore, the sidebar lacks Git status decorations, fuzzy file/ripgrep search, multi-buffer previewing, markdown/image rendering, auto-refresh, and a toggle shortcut.

## User Stories
- **US-1 (Agent-First Workspace Isolation)**: As a developer running an AI agent or shell in the main workspace (`var:role=workspace`), I want file previews and edits to open in a dedicated on-demand `var:role=preview` Kitty split so my main workspace session is never interrupted by injected keystrokes.
- **US-2 (Multi-Tab Preview & Media/Markdown Rendering)**: As a developer browsing files, I want an on-demand preview pane (`kitcode-preview`) with a buffer/tab bar (`1..9` / `[` / `]` to switch, `w` to close tab, `q` to close pane) that renders syntax-highlighted code with line numbers (and optional highlighted target line), formatted Markdown (with `m` raw-source toggle), and inline images (`kitty +kitten icat` + image metadata), plus `e` to open the active file in `$EDITOR` (`nvim`) inside the preview/editor pane.
- **US-3 (Git Status Sidebar & Branch Header)**: As a developer working in a Git repository, I want the sidebar header to show the current Git branch + dirty count and each file/folder to display Catppuccin-colored Git status badges (`M`, `A`, `D`, `R`, `U`, `●` on parent directories).
- **US-4 (Fuzzy File Finder & Ripgrep Search)**: As a developer navigating a codebase, I want `/` in the sidebar to fuzzy-filter files by path and `g` to search file contents via `rg` (ripgrep) and jump straight to the matching file and line in the preview pane.
- **US-5 (Sidebar Toggle & Auto-Reload)**: As a developer managing screen real estate, I want `Alt+B` / `Ctrl+Shift+B` (`kitcode-toggle tree`) to toggle the `kitcode-tree` sidebar pane on/off and `Alt+P` (`kitcode-toggle preview`) to toggle the preview pane, and I want the tree + Git badges to auto-refresh within 2 seconds of filesystem/Git changes.
- **US-6 (Clean Repository Structure)**: As a maintainer, I want all `kitcode` code and configs in `~/Development/kitcode` with symlinks into `~/.local/bin` and `~/.config/kitty`.

## Success Criteria
1. Clicking or pressing `Enter` / `b` on a file in `kitcode-tree` opens or updates the `var:role=preview` Kitty pane (without sending text to `var:role=workspace`) and manages open files in a multi-tab bar.
2. Pressing `e` in `kitcode-tree` or `kitcode-preview` opens the file in `$EDITOR` (`nvim`) inside the preview/editor split (never injecting keystrokes into `var:role=workspace`).
3. `kitcode-tree` displays the current Git branch in the header and `M`/`A`/`U`/`D` badges on files plus `●` on modified parent folders, auto-updating on disk/Git changes.
4. Pressing `/` in `kitcode-tree` filters files by fuzzy path match; pressing `g` runs `rg --line-number` content search and opens the selected result at the matching line in `kitcode-preview`.
5. `kitcode-toggle tree` hides the sidebar if open or spawns it at 22% width on the right if closed; `Alt+B` / `Ctrl+Shift+B` and `Alt+P` are bound in `~/.config/kitty/kitty.conf`.
6. Markdown files render formatted Markdown (toggleable with `m`), and image files render metadata + inline image via `kitty +kitten icat` when supported.
