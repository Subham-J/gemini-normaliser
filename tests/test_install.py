"""Tests for install.py. Run: python3 -m unittest discover -s tests"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install  # noqa: E402


def run(*args):
    with redirect_stdout(io.StringIO()) as out:
        install.main([str(a) for a in args])
    return out.getvalue()


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.p = Path(tempfile.mkdtemp())
        (self.p / ".git").mkdir()

    def test_fresh_install(self):
        run(self.p, "--protect", "data/prices.db")
        assert (self.p / ".gemini/guard/gemini_guard.py").exists()
        assert "gemini-normaliser" in json.loads((self.p / ".agents/hooks.json").read_text())
        assert "BeforeTool" in json.loads((self.p / ".gemini/settings.json").read_text())["hooks"]
        assert json.loads((self.p / ".gemini/guard.json").read_text()) == {"protected_paths": ["data/prices.db"]}
        assert install.IMPORT_AGY in (self.p / "GEMINI.md").read_text()
        assert ".gemini/tmp/" in (self.p / ".gitignore").read_text()

    def test_merge_keeps_existing_and_is_idempotent(self):
        (self.p / ".gemini").mkdir()
        (self.p / ".agents").mkdir()
        (self.p / ".gemini/settings.json").write_text(json.dumps({
            "model": {"name": "x"},
            "hooks": {"BeforeTool": [{"matcher": "run_shell_command", "hooks": [{"type": "command", "command": "mine.sh"}]}]}}))
        (self.p / ".agents/hooks.json").write_text(json.dumps({"lint": {"PostToolUse": []}}))
        (self.p / "AGENTS.md").write_text("rules\n")
        (self.p / "GEMINI.md").write_text("my notes\n")
        run(self.p)
        run(self.p)
        s = json.loads((self.p / ".gemini/settings.json").read_text())
        assert s["model"] == {"name": "x"}
        cmds = json.dumps(s["hooks"]["BeforeTool"])
        assert "mine.sh" in cmds and cmds.count("gemini_guard.py") == 1
        h = json.loads((self.p / ".agents/hooks.json").read_text())
        assert set(h) == {"lint", "gemini-normaliser"}
        g = (self.p / "GEMINI.md").read_text()
        assert g.startswith("my notes") and g.count(install.IMPORT_AGY) == 1

    def test_uninstall_restores(self):
        (self.p / ".gemini").mkdir()
        (self.p / ".gemini/settings.json").write_text(json.dumps({"model": {"name": "x"}}))
        run(self.p)
        run(self.p, "--uninstall")
        assert json.loads((self.p / ".gemini/settings.json").read_text()) == {"model": {"name": "x"}}
        assert json.loads((self.p / ".agents/hooks.json").read_text()) == {}
        assert not (self.p / ".gemini/guard").exists()
        assert install.IMPORT_AGY not in (self.p / "GEMINI.md").read_text()

    def test_check_flags_oversized_rules(self):
        (self.p / "AGENTS.md").write_text("x" * 30000)
        out = run(self.p, "--check")
        assert "over Antigravity's 24 KB cap" in out and "missing" in out

    def test_installed_guard_passes_its_own_tests(self):
        run(self.p)
        import subprocess
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", ".gemini/guard"],
                           cwd=str(self.p), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-800:]


if __name__ == "__main__":
    unittest.main()
