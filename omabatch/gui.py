"""GTK 4 interface. Package operations run off the GTK main thread."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from . import __version__
from . import backend

CSS = """window { background: #101513; color: #e3eee8; }
headerbar { background: #101513; border-bottom: 1px solid #30473d; }
label { color: #e3eee8; }
.brand { color: #9de5be; font-size: 2.1em; font-weight: 800; }
.eyebrow { color: #9de5be; font-weight: 700; letter-spacing: 2px; }
.muted { color: #a4b9ad; }
.package-name { font-weight: 700; }
.card { background: #18221d; border: 1px solid #30473d; border-radius: 12px; padding: 14px; }
list { background: transparent; }
row { border-bottom: 1px solid #28382f; padding: 10px 8px; }
row:hover { background: #1c2a22; }
button { background: #25372d; color: #e3eee8; border-radius: 8px; padding: 8px 14px; }
button.primary { background: #9de5be; color: #102218; font-weight: 700; }
button.primary label { color: #102218; }
button:disabled { opacity: 0.45; }
checkbutton check:checked { background: #9de5be; color: #102218; }
checkbutton check { border: 1px solid #688576; border-radius: 4px; }
entry, textview, textview text { background: #141e18; color: #e3eee8; }
entry { border: 1px solid #3b5547; border-radius: 8px; padding: 8px; }
scrollbar { background: #101513; }
"""


def label(text, css=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0, wrap=wrap)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback, primary=False):
    widget = Gtk.Button(label=text)
    widget.connect("clicked", callback)
    if primary:
        widget.add_css_class("primary")
    return widget


def margins(widget, value=22):
    for side in ("top", "bottom", "start", "end"):
        getattr(widget, f"set_margin_{side}")(value)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="OmaBatch")
        self.set_default_size(1060, 800)
        self.set_size_request(700, 500)
        self.inv = None
        self.selected = set()
        self.rows = {}
        self.busy = False
        self.bulk_select = False
        self.settings_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omabatch/settings.json"
        try:
            self.font_size = max(11, min(24, int(json.loads(self.settings_path.read_text()).get("font_size", 14))))
        except (OSError, ValueError, TypeError, AttributeError):
            self.font_size = 14
        self.provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_font()

        header = Gtk.HeaderBar()
        header.set_title_widget(label("OMABATCH  /  PACKAGE MANAGER", "eyebrow"))
        self.set_titlebar(header)
        smaller = button("A−", lambda *_: self.change_font(-1))
        smaller.set_tooltip_text("Decrease text size")
        larger = button("A+", lambda *_: self.change_font(1))
        larger.set_tooltip_text("Increase text size")
        header.pack_end(larger)
        header.pack_end(smaller)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        margins(root)
        self.set_child(root)
        title_row = Gtk.Box(spacing=16)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        titles.append(label("OmaBatch", "brand"))
        titles.append(label("A little less clutter. A little more space.", "muted"))
        title_row.append(titles)
        self.refresh_button = button("Refresh", lambda *_: self.refresh())
        title_row.append(self.refresh_button)
        root.append(title_row)
        note = label("Choose packages. Review the list. You stay in control.\nCore packages are protected. No automatic dependency or personal-data cleanup.", wrap=True)
        note.add_css_class("card")
        root.append(note)

        self.controls = Gtk.Box(spacing=10)
        self.search = Gtk.SearchEntry(hexpand=True)
        self.search.set_placeholder_text("Search installed packages…")
        self.search.connect("search-changed", lambda *_: self.filter_rows())
        self.controls.append(self.search)
        self.scope = Gtk.DropDown.new_from_strings(["Explicitly installed", "All packages", "Foreign / AUR"])
        self.scope.connect("notify::selected", lambda *_: self.filter_rows())
        self.controls.append(self.scope)
        root.append(self.controls)
        self.selection_controls = Gtk.Box(spacing=10)
        self.selection_controls.append(button("Select all visible", self.select_visible))
        self.selection_controls.append(button("Clear selection", self.clear_selection))
        self.count = label("Reading packages…", "muted", True)
        self.count.set_hexpand(True)
        self.count.set_xalign(1)
        self.selection_controls.append(self.count)
        root.append(self.selection_controls)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_filter_func(self.matches)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.listbox)
        root.append(scroll)
        footer = Gtk.Box(spacing=16)
        self.status = label("Loading installed packages…", "muted", True)
        self.status.set_hexpand(True)
        self.status.set_selectable(True)
        footer.append(self.status)
        self.review_button = button("Review removal →", self.review, primary=True)
        self.review_button.set_sensitive(False)
        footer.append(self.review_button)
        root.append(footer)
        self.connect("close-request", self.on_close)
        self.refresh()

    def on_close(self, *_):
        if self.busy:
            self.status.set_text("Wait for the current operation. If pacman is open, finish or cancel there first.")
            return True
        return False

    def apply_font(self):
        self.provider.load_from_data((CSS + f"\nwindow {{ font-size: {self.font_size}px; }}").encode())

    def change_font(self, delta):
        self.font_size = max(11, min(24, self.font_size + delta))
        self.apply_font()
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps({"font_size": self.font_size}))
        except OSError:
            self.status.set_text("Text size changed for this session; preferences could not be saved.")

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.controls, self.selection_controls, self.listbox, self.refresh_button):
            widget.set_sensitive(not busy)
        self.update_count()

    def background(self, work, done):
        self.set_busy(True)
        def worker():
            try:
                result = work()
            except Exception as error:
                GLib.idle_add(self.failed, str(error))
            else:
                GLib.idle_add(self.completed, done, result)
        threading.Thread(target=worker, daemon=True).start()

    def completed(self, done, result):
        self.set_busy(False)
        done(result)
        return False

    def failed(self, message):
        self.set_busy(False)
        self.status.set_text("Stopped safely: " + message)
        return False

    def refresh(self):
        if not self.busy:
            self.status.set_text("Reading installed packages…")
            self.background(backend.inventory, self.populate)

    def populate(self, inv):
        self.inv = inv
        self.selected.clear()
        self.rows.clear()
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)
        for name in sorted(inv.records):
            info = inv.records[name]
            row = Gtk.ListBoxRow()
            row.package_name = name
            box = Gtk.Box(spacing=14)
            row.set_child(box)
            check = Gtk.CheckButton()
            check.set_tooltip_text("Select " + name)
            check.set_sensitive(name not in inv.protected)
            check.connect("toggled", self.toggled, name)
            box.append(check)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
            text.append(label(name, "package-name"))
            description = label(info.get("Description", ""), "muted")
            description.set_ellipsize(3)  # Pango.EllipsizeMode.END
            description.set_max_width_chars(65)
            text.append(description)
            box.append(text)
            source = "Foreign / AUR" if name in inv.foreign else "Pacman"
            meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            meta.append(label("Protected" if name in inv.protected else source, "muted"))
            meta.append(label(info.get("Installed Size", ""), "muted"))
            box.append(meta)
            box.append(button("Info", lambda *_, n=name: self.show_info(n)))
            self.rows[name] = (row, check)
            self.listbox.append(row)
        self.filter_rows()
        self.status.set_text(f"{len(inv.records)} installed · {len(inv.protected)} protected. Nothing selected.")

    def matches(self, row):
        if self.inv is None:
            return False
        name = row.package_name
        info = self.inv.records[name]
        query = self.search.get_text().casefold().strip()
        scope = self.scope.get_selected()
        return ((not query or query in (name + " " + info.get("Description", "")).casefold())
                and (scope != 0 or info.get("Install Reason") == "Explicitly installed")
                and (scope != 2 or name in self.inv.foreign))

    def filter_rows(self):
        self.listbox.invalidate_filter()
        self.update_count()

    def update_count(self):
        visible = sum(1 for row, _ in self.rows.values() if self.matches(row))
        hidden = sum(1 for name in self.selected if name in self.rows and not self.matches(self.rows[name][0]))
        self.count.set_text(f"{visible} visible · {len(self.selected)} selected" + (f" ({hidden} hidden by filter)" if hidden else ""))
        self.review_button.set_sensitive(bool(self.selected) and not self.busy)

    def toggled(self, check, name):
        if check.get_active():
            self.selected.add(name)
        else:
            self.selected.discard(name)
        if not self.bulk_select:
            self.update_count()

    def select_visible(self, *_):
        if self.inv:
            self.bulk_select = True
            for name, (row, check) in self.rows.items():
                if self.matches(row) and name not in self.inv.protected:
                    check.set_active(True)
            self.bulk_select = False
            self.update_count()

    def clear_selection(self, *_):
        self.bulk_select = True
        for _, check in self.rows.values():
            check.set_active(False)
        self.bulk_select = False
        self.update_count()

    def text_window(self, title, text):
        dialog = Gtk.Window(title=title, transient_for=self, modal=True,
                            default_width=740, default_height=570)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        margins(root)
        dialog.set_child(root)
        view = Gtk.TextView(editable=False, cursor_visible=False,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        margins(view, 12)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=view)
        root.append(scroll)
        actions = Gtk.Box(spacing=12, halign=Gtk.Align.END)
        actions.append(button("Close", lambda *_: dialog.close()))
        root.append(actions)
        return dialog, actions

    def show_info(self, name):
        reason = self.inv.protected.get(name, "Selectable; pacman dependency checks still apply.")
        source = "Foreign package (may be AUR or manually installed)" if name in self.inv.foreign else "Installed pacman package"
        text = f"{source}\n\n{reason}\n\n{self.inv.records[name]['_raw']}"
        dialog, _ = self.text_window(name + " — Package information", text)
        dialog.present()

    def review(self, *_):
        names = sorted(self.selected)
        expected = self.inv.fingerprint
        self.status.set_text("Checking dependencies and the exact removal list…")
        def work():
            inv = backend.inventory()
            if inv.fingerprint != expected:
                raise backend.SafetyError("Installed packages changed. Refresh before reviewing.")
            return backend.preview(names, inv)
        self.background(work, self.show_review)

    def show_review(self, plan):
        lines = ["REVIEW REMOVAL", "", f"{len(plan['packages'])} packages selected:", ""]
        for name in plan["packages"]:
            info = self.inv.records[name]
            lines.append(f"• {name}  {info['Version']}  ({info.get('Installed Size', '?')})")
        lines += ["", "Only these packages will be requested. No dependency cleanup or cascade.",
                  "Personal files are kept. Pacman preserves eligible modified configuration as .pacsave.",
                  "Package uninstall scripts and system hooks may run and can change system files/services.",
                  "Optional integrations may stop working when an optional dependency is removed.",
                  "", "Next: a terminal opens for your password and pacman's final confirmation.",
                  "You can still answer n to cancel. This review expires after 15 minutes."]
        if plan["warnings"]:
            lines += ["", "Pacman warnings:", plan["warnings"]]
        dialog, actions = self.text_window("Review removal — OmaBatch", "\n".join(lines))
        proceed = button("Open pacman confirmation", lambda *_: self.start_removal(dialog, plan), primary=True)
        actions.append(proceed)
        self.status.set_text("Review ready. No packages have been removed.")
        dialog.present()

    def start_removal(self, dialog, plan):
        try:
            path = backend.save_plan(plan)
            command = backend.terminal_command(Path(__file__).resolve().parent.parent / "launch.py", path)
        except (OSError, backend.SafetyError) as error:
            self.failed(str(error))
            return
        dialog.close()
        self.status.set_text("Finish or cancel in the pacman terminal, then return here.")
        def work():
            try:
                return subprocess.run(command, check=False).returncode
            finally:
                # The terminal helper has already read the review. Expired files
                # left after a crash are harmless and never execute themselves.
                Path(path).unlink(missing_ok=True)
        self.background(work, lambda code: self.refresh())


def main():
    if os.geteuid() == 0:
        print("Run OmaBatch as your normal desktop user, without sudo.", file=sys.stderr)
        return 1
    app = Gtk.Application(application_id="io.github.RobbieUK1.OmaBatch")
    app.connect("activate", lambda application: (Window(application).present()
                if not application.get_active_window() else application.get_active_window().present()))
    return app.run([sys.argv[0]])
