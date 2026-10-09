"""Builds the native window shell for real (skipped without swift and the SDK)."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_SH = ROOT / "ui-shell" / "build.sh"


def _have_toolchain() -> bool:
    if shutil.which("swift") is None:
        return False
    try:
        sdk = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return sdk.returncode == 0 and Path(sdk.stdout.strip()).exists()


@unittest.skipUnless(_have_toolchain(), "swift or the macOS SDK is not available")
class UiShellBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.out = Path(cls.tmp.name) / "out"
        env = dict(os.environ, GH_DEPLOY_WATCHER_BUILD_DIR=str(cls.out))
        cls.build = subprocess.run(["bash", str(BUILD_SH)], env=env, capture_output=True,
                                   text=True, timeout=900)
        cls.app = cls.out / "GhDeployWatcher.app"
        cls.exe = cls.app / "Contents" / "MacOS" / "GhDeployWatcher"

    def test_build_succeeds_and_prints_bundle_path(self) -> None:
        self.assertEqual(self.build.returncode, 0, self.build.stderr)
        self.assertIn(str(self.app), self.build.stdout)

    def test_bundle_layout(self) -> None:
        self.assertTrue(self.exe.is_file())
        self.assertTrue(os.access(str(self.exe), os.X_OK))
        self.assertTrue((self.app / "Contents" / "Info.plist").is_file())

    def test_info_plist(self) -> None:
        with open(str(self.app / "Contents" / "Info.plist"), "rb") as fh:
            info = plistlib.load(fh)
        self.assertEqual(info["CFBundleIdentifier"], "dev.gh-deploy-watcher.ui")
        self.assertEqual(info["CFBundleName"], "GhDeployWatcher")
        self.assertEqual(info["CFBundleExecutable"], "GhDeployWatcher")
        self.assertEqual(info["CFBundlePackageType"], "APPL")
        self.assertTrue(info["NSAppTransportSecurity"]["NSAllowsLocalNetworking"])
        self.assertNotIn("LSUIElement", info)
        self.assertIn("LSMinimumSystemVersion", info)

    def test_signed(self) -> None:
        r = subprocess.run(["codesign", "--verify", "--strict", str(self.app)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_build_is_idempotent(self) -> None:
        env = dict(os.environ, GH_DEPLOY_WATCHER_BUILD_DIR=str(self.out))
        r = subprocess.run(["bash", str(BUILD_SH)], env=env, capture_output=True, text=True, timeout=900)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.exe.is_file())

    def test_selftest_passes(self) -> None:
        r = subprocess.run([str(self.exe), "--selftest"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("menu checks ok", r.stdout)

    def test_bad_urls_are_refused_without_a_window(self) -> None:
        for line in ["", "http://localhost:80/", "file:///etc/passwd", "http://127.0.0.1:0/",
                     "http://evil@127.0.0.1:80/", "http://127.0.0.1:80/ x",
                     "http://127.0.0.1:80@evil.com/", "http://evil.com#@127.0.0.1:80/"]:
            with self.subTest(line=line):
                r = subprocess.run([str(self.exe)], input=(line + "\n").encode(),
                                   capture_output=True, timeout=30)
                self.assertEqual(r.returncode, 2)
                self.assertIn(b"refusing URL", r.stderr)

    def test_missing_stdin_line_is_refused(self) -> None:
        r = subprocess.run([str(self.exe)], input=b"", capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"no URL on stdin", r.stderr)


if __name__ == "__main__":
    unittest.main()
