#!/usr/bin/env bash
# Installs OmaBatch for the current user. No sudo, package changes or deletions.
set -euo pipefail
VERSION=1.0.0
REPOSITORY=RobbieUK1/OmaBatch
PREFIX=${OMABATCH_PREFIX:-"$HOME/.local"}

die() { printf 'OmaBatch: %s\n' "$*" >&2; exit 1; }
[[ $EUID -ne 0 ]] || die 'Run this installer as your normal user, without sudo.'
[[ $PREFIX = /* && $PREFIX != *$'\n'* && $PREFIX != *$'\r'* ]] || die 'Installation prefix must be an absolute, single-line path.'
for tool in curl tar sha256sum python3 pacman sudo; do
  command -v "$tool" >/dev/null || die "Missing $tool. On Arch: sudo pacman -S --needed python python-gobject gtk4 curl tar sudo alacritty"
done
python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' 2>/dev/null ||
  die 'GTK 4 Python bindings missing. Install: sudo pacman -S --needed python-gobject gtk4'
found_terminal=false
for terminal in alacritty foot kitty xterm; do
  if command -v "$terminal" >/dev/null; then found_terminal=true; break; fi
done
$found_terminal || die 'Install a supported terminal: sudo pacman -S --needed alacritty'

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
if [[ ${1:-} == --local && $# == 2 ]]; then
  source_dir=$(cd -- "$2" && pwd)
  cp -R -- "$source_dir/omabatch" "$source_dir/launch.py" "$source_dir/assets" "$tmp/"
elif [[ $# == 0 ]]; then
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
    "https://codeload.github.com/$REPOSITORY/tar.gz/refs/tags/v$VERSION" -o "$tmp/source.tar.gz"
  tar -xzf "$tmp/source.tar.gz" --strip-components=1 -C "$tmp"
else
  die 'Usage: install.sh [--local /absolute/path/to/OmaBatch]'
fi
[[ -f $tmp/launch.py && -f $tmp/omabatch/backend.py && -f $tmp/assets/omabatch.svg ]] || die 'Incomplete archive.'
actual=$(python3 "$tmp/launch.py" --version)
[[ $actual == "OmaBatch $VERSION" ]] || die 'Release version mismatch.'

app="$PREFIX/share/omabatch"
mkdir -p -- "$app/versions" "$PREFIX/bin" "$PREFIX/share/applications" "$PREFIX/share/icons/hicolor/scalable/apps"
# Content-addressed releases keep upgrades atomic and previous versions intact.
digest=$(cat "$tmp/launch.py" "$tmp/omabatch/"*.py "$tmp/assets/omabatch.svg" | sha256sum)
digest=${digest%% *}
release="$app/versions/$VERSION-$digest"
if [[ ! -d $release ]]; then
  stage=$(mktemp -d "$app/versions/.staging.XXXXXX")
  cp -R -- "$tmp/omabatch" "$tmp/launch.py" "$tmp/assets" "$stage/"
  find "$stage" -name __pycache__ -type d -exec rm -rf -- {} +
  mv -- "$stage" "$release"
fi
python3 - "$PREFIX" "$tmp" <<'PY'
from pathlib import Path
import shlex
import sys
prefix, temp = map(Path, sys.argv[1:])
script = prefix / "share/omabatch/current/launch.py"
launcher = temp / "launcher.sh"
launcher.write_text('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(script)) + ' "$@"\n')
launcher.chmod(0o755)
def desktop_escape(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
exec_path = desktop_escape(prefix / 'bin/omabatch')
icon = str(prefix / 'share/icons/hicolor/scalable/apps/omabatch.svg')
(temp / 'io.github.RobbieUK1.OmaBatch.desktop').write_text(
    '[Desktop Entry]\nType=Application\nName=OmaBatch\n'
    'Comment=Review and remove installed packages safely\n'
    f'Exec="{exec_path}"\nIcon={icon}\nTerminal=false\n'
    'Categories=System;Settings;PackageManager;\nKeywords=uninstall;packages;omarchy;arch;\n')
PY
ln -s -- "$release" "$tmp/current"
mv -Tf -- "$tmp/current" "$app/current"
install -m 644 -- "$tmp/assets/omabatch.svg" "$PREFIX/share/icons/hicolor/scalable/apps/omabatch.svg"
mv -f -- "$tmp/launcher.sh" "$PREFIX/bin/omabatch"
mv -f -- "$tmp/io.github.RobbieUK1.OmaBatch.desktop" "$PREFIX/share/applications/"
if command -v update-desktop-database >/dev/null; then
  update-desktop-database "$PREFIX/share/applications" >/dev/null 2>&1 || true
fi
printf '\nOmaBatch %s installed. Open OmaBatch from your app launcher, or run:\n  %q\n' "$VERSION" "$PREFIX/bin/omabatch"
