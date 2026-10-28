"""Exercise actual GTK widgets on a virtual display, without removing anything."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk
from omabatch import backend
from omabatch.gui import Window


def pump(predicate, timeout=15):
    start = time.monotonic()
    while not predicate():
        GLib.MainContext.default().iteration(False)
        if time.monotonic() - start > timeout:
            raise AssertionError("GTK operation timed out")
        time.sleep(.01)
    for _ in range(20):
        GLib.MainContext.default().iteration(False)


def smoke():
    raw = """Name            : {name}
Version         : 1.0-1
Description     : {description}
Groups          : None
Provides        : None
Depends On      : None
Installed Size  : 12.5 MiB
Install Reason  : Explicitly installed
"""
    samples = [("alacritty", "A fast, cross-platform terminal emulator"),
               ("audacity", "Audio editor and recorder"),
               ("blender", "A fully integrated 3D graphics creation suite"),
               ("chromium", "The open-source browser project"),
               ("discord", "Voice, video and text chat"),
               ("gimp", "GNU image manipulation program"),
               ("hyprland", "Dynamic tiling Wayland compositor"),
               ("inkscape", "Professional vector graphics editor"),
               ("kdenlive", "Non-linear video editor"),
               ("obs-studio", "Video recording and live streaming"),
               ("steam", "Digital games platform"),
               ("vlc", "Multimedia player and framework")]
    records = backend.parse_info("\n".join(raw.format(name=n, description=d) for n, d in samples))
    inv = backend.Inventory(records, {"discord"}, backend.protected_packages(records), "demo")
    app = Gtk.Application(application_id="io.github.RobbieUK1.OmaBatch.Smoke")
    app.register(None)
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), patch.object(backend, "inventory", return_value=inv):
        window = Window(app)
        window.present()
        pump(lambda: window.inv is not None and not window.busy)
        assert not window.rows["alacritty"][1].get_sensitive()
        assert not window.review_button.get_sensitive()
        window.rows["audacity"][1].set_active(True)
        assert window.review_button.get_sensitive()
        window.search.set_text("chromium")
        window.filter_rows()
        assert "1 hidden" in window.count.get_text()
        window.select_visible()
        assert window.selected == {"audacity", "chromium"}
        window.clear_selection()
        assert not window.selected
        window.search.set_text("")
        window.scope.set_selected(2)
        window.select_visible()
        assert window.selected == {"discord"}
        window.clear_selection()
        window.scope.set_selected(0)
        window.select_visible()
        assert not (window.selected & inv.protected.keys())
        window.clear_selection()
        previous = window.font_size
        window.change_font(1)
        assert window.font_size == previous + 1
        assert json_font(tmp) == previous + 1
        window.change_font(-1)
        window.show_info("alacritty")
        pump(lambda: len(Gtk.Window.get_toplevels()) >= 2)
        for dialog in list(Gtk.Window.get_toplevels()):
            if dialog != window:
                dialog.close()
        window.rows["audacity"][1].set_active(True)
        window.rows["blender"][1].set_active(True)
        plan = {"schema": 1, "created": time.time(), "fingerprint": "demo",
                "packages": ["audacity", "blender"], "warnings": "Example optional dependency warning"}
        with patch.object(backend, "preview", return_value=plan):
            window.review()
            pump(lambda: not window.busy and window.status.get_text().startswith("Review ready"))
        for dialog in list(Gtk.Window.get_toplevels()):
            if dialog != window:
                dialog.close()
        if len(sys.argv) == 2:
            # Optional screenshot for README; test itself has no ImageMagick dependency.
            pump(lambda: window.get_mapped())
            time.sleep(.3)
            subprocess.run(["import", "-window", "root", sys.argv[1]], check=True)
        window.close()
    print("GTK smoke passed: search, filters, protected selection, hidden selections, font persistence, information and review.")


def json_font(tmp):
    import json
    return json.loads((Path(tmp) / "omabatch/settings.json").read_text())["font_size"]


if __name__ == "__main__":
    smoke()
