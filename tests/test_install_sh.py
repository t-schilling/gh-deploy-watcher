"""Runs the hermetic bash tests for install.sh as part of unittest discovery."""
import os
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class InstallShTest(unittest.TestCase):
    def test_install_sh(self):
        proc = subprocess.run(
            ["bash", os.path.join(HERE, "test_install.sh")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)


if __name__ == "__main__":
    unittest.main()
