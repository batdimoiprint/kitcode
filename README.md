# kitcode

Agent-first IDE native to the [Kitty](https://sw.kovidgoyal.net/kitty/) terminal, themed with Catppuccin Mocha and Nerd Font icons.

`kitcode` separates your main AI/shell workspace (`var:role=workspace`) from file browsing, previewing, and editing so clicking files never injects keystrokes into a running agent (`claude`, `codex`, `agy`) or server.

## Features

- **Agent-First Workspace Isolation**: Previews open in an on-demand `var:role=preview` split and edits open in a dedicated `var:role=editor` split (`nvim --listen` remote tabs).
- **Git-Aware Sidebar (`kitcode-tree`)**: Shows current branch + dirty count in the header, `[M]`/`[A]`/`[U]`/`[D]`/`[R]` file badges, dirty parent folder indicators (`●`), and auto-reloads on filesystem/Git changes.
- **Built-in Fuzzy File & Ripgrep Search**: Press `/` to fuzzy-find files or `g` for live `rg` content search with direct line-number jump.
- **Multi-Tab Preview Pane (`kitcode-preview`)**: Numbered buffer bar (`1-9`, `[`, `]`), syntax-highlighted code with line numbers, formatted Markdown (`m` to toggle raw source), and inline image preview (`i` via `kitty +kitten icat`).
- **Pane Toggles (`kitcode-toggle`)**: Toggle the file tree (`Alt+B` / `Ctrl+Shift+B`) or preview pane (`Alt+P`) from anywhere in Kitty.

## Requirements

- `kitty` (with `allow_remote_control yes` and `listen_on unix:/tmp/kitty-socket`)
- Python 3.10+ with `textual`, `rich`, `pygments`
- Optional CLI tools: `git`, `rg` (ripgrep), `nvim`

## Installation

```bash
./install.sh
```

Symlinks `kitcode`, `kitcode-tree`, `kitcode-preview`, and `kitcode-toggle` to `~/.local/bin/`, links `config/kitcode.session` to `~/.config/kitty/kitcode.session`, and adds toggle keybindings to `~/.config/kitty/kitty.conf`.

## Usage

```bash
kitcode [directory]
```

## Keybindings

| Context | Key | Action |
|---|---|---|
| **Global (Kitty)** | `Alt+B` / `Ctrl+Shift+B` | Toggle `kitcode-tree` sidebar |
| **Global (Kitty)** | `Alt+P` | Toggle `kitcode-preview` pane |
| **Sidebar (`kitcode-tree`)** | `Click` / `Enter` / `b` | Open file in `kitcode-preview` tab |
| **Sidebar (`kitcode-tree`)** | `/` | Fuzzy file search |
| **Sidebar (`kitcode-tree`)** | `g` | Ripgrep content search |
| **Sidebar / Preview** | `e` | Open file in `$EDITOR` (`nvim` server tab) |
| **Sidebar (`kitcode-tree`)** | `h` / `.` | Toggle hidden dotfiles |
| **Sidebar (`kitcode-tree`)** | `r` / `q` | Refresh tree / Quit sidebar |
| **Preview (`kitcode-preview`)** | `1..9` / `[` / `]` | Switch buffer tabs |
| **Preview (`kitcode-preview`)** | `m` / `i` | Toggle raw Markdown / `icat` image preview |
| **Preview (`kitcode-preview`)** | `w` / `q` | Close active tab / Close preview pane |

## Tests

```bash
python3 -m unittest discover -s tests -v
```
