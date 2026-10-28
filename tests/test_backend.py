import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from omabatch import backend as b


def record(name, depends="None", provides="None", **extra):
    return {"Name": name, "Version": "1.0-1", "Depends On": depends,
            "Provides": provides, "Groups": "None", "Install Reason": "Explicitly installed",
            "Description": "A package", "Installed Size": "1 MiB", "_raw": name, **extra}


def fixture():
    records = {n: record(n) for n in ("base", "bash", "glibc", "editor", "game")}
    records["base"]["Depends On"] = "bash>=5"
    records["bash"]["Depends On"] = "glibc"
    return b.Inventory(records, {"game"}, b.protected_packages(records), "db-hash")


def plan():
    return {"schema": 1, "created": time.time(), "fingerprint": "db-hash",
            "packages": ["editor"], "warnings": ""}


class SafetyTests(unittest.TestCase):
    def test_parse_wrapped_fields(self):
        text = """Name            : editor
Version         : 1.0-1
Description     : An editor
                  with wrapped description
Groups          : None
Provides        : virtual-editor=1
Depends On      : glibc
                  bash>=5
Install Reason  : Explicitly installed
"""
        info = b.parse_info(text)["editor"]
        self.assertEqual(info["Depends On"], "glibc bash>=5")
        self.assertEqual(info["Description"], "An editor with wrapped description")

    def test_incomplete_discovery_refused(self):
        for text in ("", "Name : editor\nVersion : 1"):
            with self.subTest(text=text), self.assertRaises(b.SafetyError):
                b.parse_info(text)

    def test_core_dependency_closure(self):
        self.assertEqual(set(fixture().protected), {"base", "bash", "glibc"})

    def test_all_virtual_providers_conservatively_protected(self):
        records = {"base": record("base", "virtual>=1"),
                   "one": record("one", provides="virtual=1"),
                   "two": record("two", provides="virtual=2")}
        self.assertEqual(set(b.protected_packages(records)), {"base", "one", "two"})

    def test_extra_owner_and_dependencies_protected(self):
        records = {"custom-kernel": record("custom-kernel", "module"), "module": record("module")}
        self.assertEqual(set(b.protected_packages(records, {"custom-kernel"})), set(records))

    def test_unresolved_core_dependency_refused(self):
        with self.assertRaises(b.SafetyError):
            b.protected_packages({"base": record("base", "missing")})

    def test_custom_kernel_desktop_patterns(self):
        for name in ("linux-custom", "omarchy-settings", "hyprland-git", "limine-tools"):
            with self.subTest(name=name):
                self.assertIn(name, b.protected_packages({name: record(name)}))

    def test_bad_targets_never_reach_pacman(self):
        for names in (["base"], ["missing"], [], ["editor", "editor"],
                      ["--nodeps"], ["editor; touch /tmp/bad"], ["editor\n"], [None], None, "editor"):
            with self.subTest(names=names), patch.object(b, "run") as run, self.assertRaises(b.SafetyError):
                b.preview(names, fixture())
            run.assert_not_called()

    @patch.object(b, "run")
    def test_exact_preview_and_optional_warnings(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "editor\ngame\n", "optional warning")
        result = b.preview(["game", "editor"], fixture())
        self.assertEqual(result["packages"], ["editor", "game"])
        self.assertEqual(result["warnings"], "optional warning")
        run.assert_called_once_with([b.PACMAN, "-R", "--print-format", "%n", "--", "editor", "game"])

    @patch.object(b, "run")
    def test_unexpected_extra_missing_and_duplicate_targets_refused(self, run):
        for output in ("editor\nbase\n", "", "editor\neditor\n"):
            run.return_value = subprocess.CompletedProcess([], 0, output, "")
            with self.subTest(output=output), self.assertRaises(b.SafetyError):
                b.preview(["editor"], fixture())

    @patch.object(b, "run", side_effect=b.SafetyError("required by other package"))
    def test_dependency_failure_propagates(self, _):
        with self.assertRaisesRegex(b.SafetyError, "required"):
            b.preview(["editor"], fixture())

    def test_expired_future_invalid_and_changed_review_refused(self):
        for changes in ({"created": time.time() - 901}, {"created": time.time() + 10},
                        {"schema": 7}, {"fingerprint": "changed"}, {"created": "yesterday"}):
            with self.subTest(changes=changes), patch.object(b, "run") as run, self.assertRaises(b.SafetyError):
                b.revalidate({**plan(), **changes}, fixture())
            run.assert_not_called()

    def test_saved_plan_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}):
            value = plan()
            path = Path(b.save_plan(value))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(b.load_plan(path), value)

    def test_symlink_and_writable_review_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan"
            path.write_text(json.dumps(plan()))
            path.chmod(0o666)
            with self.assertRaises(b.SafetyError):
                b.load_plan(path)
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                b.load_plan(link)

    @patch.object(b.os, "geteuid", return_value=0)
    @patch.object(b.subprocess, "run")
    def test_root_execution_refused(self, run, _):
        with self.assertRaises(b.SafetyError):
            b.execute_plan("anything")
        run.assert_not_called()

    @patch.object(b.os, "geteuid", return_value=1000)
    @patch.object(b.sys.stdin, "isatty", return_value=False)
    @patch.object(b.subprocess, "run")
    def test_noninteractive_execution_refused(self, run, *_):
        with self.assertRaises(b.SafetyError):
            b.execute_plan("anything")
        run.assert_not_called()

    @patch.object(b.os, "geteuid", return_value=1000)
    @patch.object(b.sys.stdin, "isatty", return_value=True)
    @patch.object(b, "load_plan", side_effect=b.SafetyError("invalid"))
    @patch.object(b.subprocess, "run")
    def test_invalid_review_never_invokes_sudo(self, run, *_):
        with self.assertRaises(b.SafetyError):
            b.execute_plan("review")
        run.assert_not_called()

    @patch.object(b.os, "geteuid", return_value=1000)
    @patch.object(b.sys.stdin, "isatty", return_value=True)
    @patch.object(b, "load_plan", return_value=plan())
    @patch.object(b, "inventory", return_value=fixture())
    @patch.object(b, "revalidate", return_value=plan())
    @patch.object(b.subprocess, "run", return_value=subprocess.CompletedProcess([], 0))
    @patch("builtins.input", return_value="")
    def test_privileged_command_is_plain_interactive_pacman_only(self, _, run, *rest):
        self.assertEqual(b.execute_plan("review"), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/sudo", b.PACMAN, "-R", "--confirm", "--", "editor"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("input", kwargs)
        self.assertNotIn("stdin", kwargs)

    @patch.object(b.shutil, "which", return_value=None)
    def test_no_terminal_refused(self, _):
        with self.assertRaises(b.SafetyError):
            b.terminal_command("/path/with spaces/launch.py", "review")

    @patch.object(b.shutil, "which", side_effect=lambda n: "/usr/bin/foot" if n == "foot" else None)
    def test_terminal_argv_preserves_spaces(self, _):
        command = b.terminal_command("/path with spaces/launch.py", "/review with spaces.json")
        self.assertEqual(command, ["/usr/bin/foot", b.sys.executable, "/path with spaces/launch.py",
                                   "--execute", "/review with spaces.json"])

    @patch.object(b.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "database locked"))
    def test_command_errors_are_visible(self, _):
        with self.assertRaisesRegex(b.SafetyError, "locked"):
            b.run([b.PACMAN, "-Qi"])


if __name__ == "__main__":
    unittest.main()
