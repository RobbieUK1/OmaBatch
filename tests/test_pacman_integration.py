"""Real pacman preview tests in a disposable database; never touch host packages."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from omabatch import backend as b


@unittest.skipUnless(shutil.which("pacman"), "Requires Arch pacman")
class PacmanPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)
        local = self.db / "local"
        local.mkdir()
        (local / "ALPM_DB_VERSION").write_text("9\n")
        for name, deps in (("base", ["shell"]), ("shell", []),
                           ("editor", ["shared"]), ("shared", []), ("game", ["shared"])):
            package = local / f"{name}-1.0-1"
            package.mkdir()
            content = f"%NAME%\n{name}\n\n%VERSION%\n1.0-1\n\n%DESC%\nTest package\n\n%ARCH%\nx86_64\n\n%SIZE%\n1024\n\n%REASON%\n0\n\n"
            if deps:
                content += "%DEPENDS%\n" + "\n".join(deps) + "\n\n"
            (package / "desc").write_text(content)
            (package / "files").write_text("%FILES%\n\n")
        self.original_run = b.run
        def isolated(args, **kwargs):
            return self.original_run([args[0], "--config", "/dev/null", "--dbpath", str(self.db), *args[1:]], **kwargs)
        self.isolated = isolated
        records = b.parse_info(isolated([b.PACMAN, "-Qi"]).stdout)
        self.inv = b.Inventory(records, set(), b.protected_packages(records), "isolated")

    def test_real_preview_removes_only_selected_application(self):
        before = {str(p): p.read_bytes() for p in self.db.rglob("*") if p.is_file()}
        with patch.object(b, "run", side_effect=self.isolated):
            result = b.preview(["editor"], self.inv)
        self.assertEqual(result["packages"], ["editor"])
        after = {str(p): p.read_bytes() for p in self.db.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_real_pacman_dependency_refusal(self):
        with patch.object(b, "run", side_effect=self.isolated), self.assertRaisesRegex(b.SafetyError, "dependenc"):
            b.preview(["shared"], self.inv)

    def test_real_batch_preview_keeps_unselected_dependency(self):
        with patch.object(b, "run", side_effect=self.isolated):
            result = b.preview(["editor", "game"], self.inv)
        self.assertEqual(result["packages"], ["editor", "game"])
        self.assertNotIn("shared", result["packages"])


if __name__ == "__main__":
    unittest.main()
