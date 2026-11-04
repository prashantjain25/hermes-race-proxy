import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


WRAPPER = Path(__file__).resolve().parents[1] / "hermes-warmup.sh"
REAL_PYTHON = Path(sys.executable)


class NativeToolSelectionTests(unittest.TestCase):
    def test_launcher_forwards_arguments_without_rewriting_tool_policy(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            home = Path(tmp)
            proxy = home / "app"
            (proxy / "config").mkdir(parents=True)
            shutil.copy2(WRAPPER, proxy / "hermes-warmup.sh")
            (proxy / "config/tools.yaml").write_text(
                "toolsets:\n  builtin:\n    terminal: {state: ON}\n"
                "  mcp:\n    demo: {state: OFF}\n"
            )
            (home / ".hermes/hermes-agent/venv/bin").mkdir(parents=True)
            (home / ".hermes/hermes-agent/venv/bin/python").symlink_to(REAL_PYTHON)
            (home / ".local/bin").mkdir(parents=True)
            (home / ".hermes/config.yaml").write_text("mcp_servers:\n  demo:\n    enabled: true\n")
            hermes = home / ".local/bin/hermes-real"
            hermes.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$HOME/calls"\n'
                'if [ "$1" = tools ] && [ "$2" = list ]; then printf "enabled terminal\\n"; fi\n'
            )
            hermes.chmod(0o755)
            bindir = home / "bin"
            bindir.mkdir()
            curl = bindir / "curl"
            curl.write_text('#!/bin/sh\nprintf 200\n')
            curl.chmod(0o755)
            env = dict(os.environ, HOME=str(home), PATH=str(bindir) + os.pathsep + os.environ["PATH"])
            cases = [(["--version"], "--version"),
                     (["--toolsets", "file,terminal"], "--toolsets file,terminal"),
                     (["chat", "--help"], "chat --help")]
            expected_calls = []
            for arguments, expected in cases:
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        ["bash", str(proxy / "hermes-warmup.sh"), *arguments],
                        env=env, text=True, capture_output=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    expected_calls.append(expected)
                    self.assertEqual((home / "calls").read_text().splitlines(), expected_calls)
            self.assertEqual((home / ".hermes/config.yaml").read_text(),
                             "mcp_servers:\n  demo:\n    enabled: true\n")


if __name__ == "__main__":
    unittest.main()
