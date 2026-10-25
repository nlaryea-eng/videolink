"""Installer contract tests without network access or package installation."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="videolink tooling ")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        scripts = self.repo / "scripts"
        scripts.mkdir()
        for name in ("install.sh", "_lib.sh"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        doctor = scripts / "doctor-subtitles.sh"
        doctor.write_text("#!/bin/bash\nexit 0\n")
        doctor.chmod(0o755)
        (self.repo / ".venv").mkdir()
        self.uv = self.repo / "fake uv"
        self.uv.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$TEST_ARGS"\nexit "${TEST_EXIT:-0}"\n')
        self.uv.chmod(0o755)
        self.args = self.repo / "args"
        self.env = dict(os.environ, VIDEOLINK_UV=str(self.uv), TEST_ARGS=str(self.args))

    def run_install(self, *args):
        return subprocess.run([str(self.repo / "scripts/install.sh"), *args],
                              env=self.env, capture_output=True, text=True)

    def test_saved_backend_survives_reinstall_from_path_with_spaces(self):
        marker = self.repo / ".venv/videolink-backend"
        marker.write_text("faster-whisper\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.args.read_text().splitlines(),
                         ["sync", "--locked", "--inexact", "--extra", "faster-whisper"])
        self.assertEqual(marker.read_text(), "faster-whisper\n")

    def test_failed_sync_does_not_change_saved_backend(self):
        marker = self.repo / ".venv/videolink-backend"
        marker.write_text("mlx\n")
        self.env["TEST_EXIT"] = "1"
        self.assertNotEqual(self.run_install("--no-backend").returncode, 0)
        self.assertEqual(marker.read_text(), "mlx\n")

    def test_invalid_backend_does_not_invoke_package_manager(self):
        self.assertEqual(self.run_install("--backend", "unknown").returncode, 2)
        self.assertFalse(self.args.exists())


if __name__ == "__main__":
    unittest.main()
