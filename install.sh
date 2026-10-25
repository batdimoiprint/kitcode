#!/usr/bin/env bash
# kitcode install script: symlinks binaries & config, updates kitty.conf idempotently

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
KITTY_CONFIG_DIR="$HOME/.config/kitty"
KITTY_CONF="$KITTY_CONFIG_DIR/kitty.conf"

mkdir -p "$BIN_DIR"
mkdir -p "$KITTY_CONFIG_DIR"

# 1. Symlink executables into ~/.local/bin
echo "Symlinking kitcode binaries into $BIN_DIR..."
ln -sf "$SCRIPT_DIR/bin/kitcode" "$BIN_DIR/kitcode"
ln -sf "$SCRIPT_DIR/bin/kitcode-tree" "$BIN_DIR/kitcode-tree"
ln -sf "$SCRIPT_DIR/bin/kitcode-preview" "$BIN_DIR/kitcode-preview"
ln -sf "$SCRIPT_DIR/bin/kitcode-toggle" "$BIN_DIR/kitcode-toggle"

# 2. Symlink kitcode.session into ~/.config/kitty
echo "Symlinking kitcode.session into $KITTY_CONFIG_DIR..."
ln -sf "$SCRIPT_DIR/config/kitcode.session" "$KITTY_CONFIG_DIR/kitcode.session"

# 3. Update ~/.config/kitty/kitty.conf idempotently
if [ -f "$KITTY_CONF" ]; then
    if ! grep -q "kitcode-toggle" "$KITTY_CONF"; then
        echo "Adding kitcode keybindings to $KITTY_CONF..."
        cat << 'EOF' >> "$KITTY_CONF"

# kitcode Pane Toggle Keybindings
map alt+b launch --type=background --cwd=current kitcode-toggle tree
map ctrl+shift+b launch --type=background --cwd=current kitcode-toggle tree
map alt+p launch --type=background --cwd=current kitcode-toggle preview
EOF
    else
        echo "kitcode keybindings already present in $KITTY_CONF."
    fi
fi

echo "kitcode installed successfully."
