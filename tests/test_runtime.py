"""Runtime config and the API-key guard (docs/SPEC.md §3)."""

import json
import shlex
import tempfile
import textwrap
import unittest
from pathlib import Path

from relayer.cli import main
from relayer.config import ConfigError
from relayer.runtime import check_environment, load_runtime

from helpers import heteronym

REPO = Path(__file__).resolve().parent.parent

GOOD = """\
version: 1
models: {fast: h, standard: s, strong: o, frontier: f}
session:
  command: ["claude", "--model", "{model}", "--settings", "{settings}"]
"""


class RuntimeConfig(unittest.TestCase):
    def load(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "relayer.yaml"
        path.write_text(textwrap.dedent(body))
        return load_runtime(path)

    def rejected(self, body, expected):
        with self.assertRaises(ConfigError) as exc:
            self.load(body)
        self.assertIn(expected, "\n".join(exc.exception.errors))

    def test_shipped_example_loads(self):
        rt = load_runtime(REPO / "examples" / "relayer.yaml")
        self.assertEqual(rt.models["frontier"], "fable")

    def test_session_argv_fills_placeholders(self):
        rt = self.load(GOOD)
        argv = rt.session_argv(heteronym(tier="strong", allow=("Read", "Bash(git *)")))
        self.assertEqual(argv[:3], ["claude", "--model", "o"])
        self.assertEqual(json.loads(argv[4]), {"permissions": {
            "allow": ["Read", "Bash(git *)"], "deny": ["Edit(./.claude/**)"]}})

    def test_missing_tier_mapping(self):
        self.rejected(GOOD.replace(" frontier: f", ""), "models.frontier: missing model name")

    def test_unknown_placeholder(self):
        self.rejected(GOOD.replace("{settings}", "{token}"), "unknown placeholder {token}")

    def test_api_key_in_session_env(self):
        self.rejected(GOOD + "  env: {ANTHROPIC_API_KEY: sk-x}\n",
                      "ANTHROPIC_API_KEY not allowed")

    def test_forbidden_names_in_pass_env(self):
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                     "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            with self.subTest(name=name):
                self.rejected(GOOD + f"  pass_env: [{name}]\n", f"{name} not allowed")

    def test_bad_env_name(self):
        self.rejected(GOOD + "  env: {'A B': x}\n", "is not a valid variable name")

    def test_program_name_with_equals(self):
        self.rejected(GOOD.replace('["claude",', '["X=1",'), "may not contain '='")

    def test_session_env_is_only_what_is_listed(self):
        rt = self.load(GOOD + "  pass_env: [KEEP]\n  env: {LIT: v}\n")
        env = rt.session_env({"HOME": "/h", "PATH": "/p", "KEEP": "k",
                              "GH_TOKEN": "t", "ANTHROPIC_API_KEY": "sk"})
        self.assertEqual(env, {"HOME": "/h", "PATH": "/p", "TERM": "screen-256color",
                               "KEEP": "k", "LIT": "v"})

    def test_path_is_required(self):
        rt = self.load(GOOD)
        with self.assertRaisesRegex(ValueError, "PATH is not set"):
            rt.session_env({"HOME": "/h"})
        rt = self.load(GOOD + "  env: {PATH: /opt/bin}\n")
        self.assertEqual(rt.session_env({})["PATH"], "/opt/bin")

    def test_command_is_shell_safe(self):
        # A trailing ";" in an argument must stay inside quotes: tmux treats a
        # bare trailing ";" as a command separator.
        rt = self.load(GOOD.replace('"{settings}"]', '"{settings}", "x;"]'))
        cmd = rt.session_command(heteronym(allow=("Bash(echo $HOME; rm *)",)), {"PATH": "/p"})
        self.assertFalse(cmd.endswith(";"))
        words = shlex.split(cmd)
        self.assertEqual(words[-1], "x;")
        self.assertEqual(json.loads(words[-2])["permissions"]["allow"],
                         ["Bash(echo $HOME; rm *)"])

    def test_session_hash_tracks_runtime_changes(self):
        p = heteronym()
        a = self.load(GOOD)
        b = self.load(GOOD.replace("strong: o", "strong: o2").replace("standard: s", "standard: s2"))
        c = self.load(GOOD + "  env: {X: '1'}\n")
        d = self.load(GOOD + "  pass_env: [KEEP]\n")
        self.assertNotEqual(a.session_hash(p), b.session_hash(p))
        self.assertNotEqual(a.session_hash(p), c.session_hash(p))
        self.assertNotEqual(a.session_hash(p), d.session_hash(p))

    def test_session_hash_ignores_inherited_values(self):
        # Hashing PATH's value would restart every session whenever a
        # reconcile ran from a different shell.
        import inspect
        self.assertNotIn("environ", inspect.signature(self.load(GOOD).session_hash).parameters)

    def test_bad_socket(self):
        self.rejected(GOOD + "tmux_socket: ../x\n", "tmux_socket: must match")

    def test_unknown_key(self):
        self.rejected(GOOD + "sesion: {}\n", "sesion: unknown key")


class ApiKeyGuard(unittest.TestCase):
    def test_flags_key_in_environment(self):
        self.assertEqual(len(check_environment({"ANTHROPIC_API_KEY": "sk-x"})), 1)
        self.assertEqual(len(check_environment({"CLAUDE_CODE_USE_BEDROCK": "1"})), 1)
        self.assertEqual(check_environment({"ANTHROPIC_API_KEY": ""}), [])
        self.assertEqual(check_environment({"PATH": "/bin"}), [])

    def test_reconcile_refuses_before_touching_sessions(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}), \
                mock.patch("relayer.cli.TmuxBackend") as backend, \
                mock.patch("sys.stderr"):
            code = main(["reconcile", "--heteronyms", str(REPO / "examples/heteronyms.yaml"),
                         "--templates", str(REPO / "templates"),
                         "--runtime", str(REPO / "examples/relayer.yaml")])
        self.assertEqual(code, 2)
        backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
