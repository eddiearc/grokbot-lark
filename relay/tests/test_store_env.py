#!/usr/bin/env python3
import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cliutil import load_store_env_file, ALLOWED_STORE_KEYS


class StoreEnvTest(unittest.TestCase):
    def test_loads_only_allowed_dir_vars_and_expands_home_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env.sh"
            path.write_text(
                "\n".join(
                    [
                        "# comment",
                        'export PATH="${HOME}/.local/bin:${PATH}"',
                        f'export LARKSUITE_CLI_CONFIG_DIR="{tmp}/config"',
                        "export LARKSUITE_CLI_DATA_DIR=$HOME/.lark-bots/demo/data",
                        "export LARKSUITE_CLI_LOG_DIR=${HOME}/.lark-bots/demo/logs",
                        "export LARKSUITE_CLI_APP_SECRET=should-not-be-loaded",
                        "export EVIL=1",
                    ]
                )
                + "\n"
            )
            env = {"PATH": "/usr/bin"}
            load_store_env_file(path, env)
            home = str(Path.home())
            self.assertTrue(env["PATH"].startswith(home + "/.local/bin:"))
            self.assertIn("/usr/bin", env["PATH"])
            self.assertEqual(env["LARKSUITE_CLI_CONFIG_DIR"], f"{tmp}/config")
            self.assertEqual(env["LARKSUITE_CLI_DATA_DIR"], home + "/.lark-bots/demo/data")
            self.assertEqual(env["LARKSUITE_CLI_LOG_DIR"], home + "/.lark-bots/demo/logs")
            self.assertNotIn("LARKSUITE_CLI_APP_SECRET", env)
            self.assertNotIn("EVIL", env)
            self.assertEqual(
                ALLOWED_STORE_KEYS,
                {
                    "LARKSUITE_CLI_CONFIG_DIR",
                    "LARKSUITE_CLI_DATA_DIR",
                    "LARKSUITE_CLI_LOG_DIR",
                    "PATH",
                },
            )


if __name__ == "__main__":
    unittest.main()
