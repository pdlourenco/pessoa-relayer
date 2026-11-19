"""Tests for heteronyms.yaml and template validation (docs/SPEC.md §1, §2).

Every rejection rule has a case that shows it failing; the shipped examples
have a case that shows them passing.
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from relayer.config import (ConfigError, load_heteronyms, load_templates,
                            parse_duration, resolve_folder)

REPO = Path(__file__).resolve().parent.parent

ASSISTANT = """\
version: 1
max_tier: strong
default_tier: standard
folders: ["{work}/{name}"]
permissions:
  allow: [Read, Edit]
  deny: [Danger]
channel_kinds: [signal]
idle_stop: 2h
chat_creatable: true
"""


class Workspace:
    """A temp dir with a templates/ tree and a heteronyms.yaml."""

    def __init__(self, heteronyms: str, templates: dict[str, str] | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name, body in (templates or {"assistant": ASSISTANT}).items():
            d = self.root / "templates" / name
            d.mkdir(parents=True)
            (d / "template.yaml").write_text(textwrap.dedent(body))
        self.heteronyms = self.root / "heteronyms.yaml"
        self.heteronyms.write_text(textwrap.dedent(heteronyms))

    def load(self, work_root="/work"):
        templates = load_templates(self.root / "templates")
        return load_heteronyms(self.heteronyms, templates, work_root)

    def close(self):
        self._tmp.cleanup()


def heteronym_file(body: str) -> str:
    return "version: 1\nheteronyms:\n" + textwrap.indent(textwrap.dedent(body), "  ")


class ShippedExamples(unittest.TestCase):
    def test_examples_validate(self):
        templates = load_templates(REPO / "templates")
        heteronyms = load_heteronyms(REPO / "examples" / "heteronyms.yaml", templates)
        self.assertIn("pa", heteronyms)
        self.assertEqual(heteronyms["pa"].folders, ("/work/pa",))


class ValidHeteronym(unittest.TestCase):
    def test_defaults_come_from_template(self):
        ws = Workspace(heteronym_file("pa:\n  template: assistant\n"))
        self.addCleanup(ws.close)
        p = ws.load()["pa"]
        self.assertEqual(p.tier, "standard")
        self.assertEqual(p.allow, ("Read", "Edit"))
        self.assertEqual(p.deny, ("Danger",))
        self.assertEqual(p.idle_stop, 7200)
        self.assertTrue(p.enabled)
        self.assertIsNone(p.channel)

    def test_narrower_permissions_and_subfolder_allowed(self):
        ws = Workspace(heteronym_file("""\
            pa:
              template: assistant
              tier: fast
              permissions: [Read]
              folders: ["{work}/pa/notes"]
              channel: {kind: signal, chat: group.abc}
              idle_stop: never
            """))
        self.addCleanup(ws.close)
        p = ws.load()["pa"]
        self.assertEqual(p.allow, ("Read",))
        self.assertEqual(p.folders, ("/work/pa/notes",))
        self.assertEqual(p.channel.chat, "group.abc")
        self.assertIsNone(p.idle_stop)

    def _hash(self, entry: str, templates=None) -> str:
        ws = Workspace(heteronym_file(f"pa: {{template: assistant{entry}}}\n"), templates)
        self.addCleanup(ws.close)
        return ws.load()["pa"].config_hash()

    def test_hash_ignores_enabled_and_idle_stop(self):
        base = self._hash("")
        self.assertEqual(base, self._hash(", enabled: false"))
        self.assertEqual(base, self._hash(", idle_stop: 5m"))

    def test_hash_covers_session_shape(self):
        base = self._hash("")
        variants = {
            "tier": self._hash(", tier: strong"),
            "permissions": self._hash(", permissions: [Read]"),
            "folders": self._hash(', folders: ["{work}/pa/sub"]'),
            "channel": self._hash(", channel: {kind: signal, chat: g}"),
            "deny": self._hash("", {"assistant": ASSISTANT.replace("[Danger]", "[Other]")}),
        }
        for field, value in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(base, value)


class Rejections(unittest.TestCase):
    """Each case must fail validation; the message names the rule."""

    def assertRejected(self, heteronyms: str, expected: str, templates=None,
                       work_root="/work"):
        ws = Workspace(heteronyms, templates)
        self.addCleanup(ws.close)
        with self.assertRaises(ConfigError) as exc:
            ws.load(work_root)
        joined = "\n".join(exc.exception.errors)
        self.assertIn(expected, joined)

    def test_tier_above_template_cap(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, tier: frontier}\n"),
                            "exceeds template 'assistant' max_tier 'strong'")

    def test_unknown_tier(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, tier: opus}\n"),
                            "'opus' is not one of")

    def test_permission_not_in_template(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, permissions: [Bash]}\n"),
                            "'Bash' is not in template 'assistant' allow list")

    def test_folder_outside_template_root(self):
        self.assertRejected(heteronym_file('pa: {template: assistant, folders: ["{work}/other"]}\n'),
                            "is outside template 'assistant' roots")

    def test_folder_escape_with_dotdot(self):
        self.assertRejected(
            heteronym_file('pa: {template: assistant, folders: ["{work}/pa/../other"]}\n'),
            "'/work/other' is outside template")

    def test_relative_folder(self):
        self.assertRejected(heteronym_file('pa: {template: assistant, folders: ["pa"]}\n'),
                            "is not absolute")

    def test_unknown_placeholder(self):
        self.assertRejected(heteronym_file('pa: {template: assistant, folders: ["{home}/pa"]}\n'),
                            "unknown placeholder {home}")

    def test_overlapping_heteronyms(self):
        templates = {"assistant": ASSISTANT.replace('["{work}/{name}"]', '["{work}"]')}
        self.assertRejected(heteronym_file("""\
            pa: {template: assistant, folders: ["{work}/shared"]}
            pb: {template: assistant, folders: ["{work}/shared/sub"]}
            """), "overlaps heteronyms.pb folder", templates)

    def test_unknown_heteronym_key(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, permision: [Read]}\n"),
                            "heteronyms.pa.permision: unknown key")

    def test_unknown_template(self):
        self.assertRejected(heteronym_file("pa: {template: nope}\n"),
                            "unknown template 'nope'")

    def test_bad_name(self):
        self.assertRejected(heteronym_file("Bad_Name: {template: assistant}\n"),
                            "name must match")

    def test_duplicate_heteronym_key(self):
        self.assertRejected(heteronym_file("""\
            pa: {template: assistant}
            pa: {template: assistant, tier: fast}
            """), "duplicate key 'pa'")

    def test_channel_kind_not_allowed(self):
        self.assertRejected(
            heteronym_file("pa: {template: assistant, channel: {kind: telegram, chat: x}}\n"),
            "'telegram' not allowed by template 'assistant'")

    def test_channel_without_chat(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, channel: {kind: signal}}\n"),
                            "channel.chat: must be a non-empty string")

    def test_bad_idle_stop(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, idle_stop: 90}\n"),
                            "is not a duration")

    def test_wrong_version(self):
        self.assertRejected("version: 2\nheteronyms: {}\n", "version must be 1")

    def test_relative_work_root(self):
        self.assertRejected(heteronym_file("pa: {template: assistant}\n"),
                            "must be an absolute, normalised path", work_root="work")

    def test_template_default_above_max(self):
        bad = ASSISTANT.replace("default_tier: standard", "default_tier: frontier")
        self.assertRejected(heteronym_file("pa: {template: assistant}\n"),
                            "default_tier: 'frontier' exceeds max_tier 'strong'",
                            {"assistant": bad})

    def test_template_unknown_key(self):
        bad = ASSISTANT + "max_teir: fast\n"
        self.assertRejected(heteronym_file("pa: {template: assistant}\n"),
                            "template assistant.max_teir: unknown key",
                            {"assistant": bad})

    def test_sibling_prefix_is_not_inside_root(self):
        # /work/pax shares a string prefix with root /work/pa but is not in it.
        self.assertRejected(heteronym_file('pa: {template: assistant, folders: ["{work}/{name}x"]}\n'),
                            "'/work/pax' is outside template")

    def test_same_folder_twice(self):
        templates = {"assistant": ASSISTANT.replace('["{work}/{name}"]', '["{work}"]')}
        self.assertRejected(heteronym_file("""\
            pa: {template: assistant, folders: ["{work}/shared"]}
            pb: {template: assistant, folders: ["{work}/shared"]}
            """), "overlaps heteronyms.pb folder", templates)

    def test_nesting_in_the_other_direction(self):
        templates = {"assistant": ASSISTANT.replace('["{work}/{name}"]', '["{work}"]')}
        self.assertRejected(heteronym_file("""\
            pa: {template: assistant, folders: ["{work}/shared/sub"]}
            pb: {template: assistant, folders: ["{work}/shared"]}
            """), "overlaps heteronyms.pb folder", templates)

    def test_double_slash_cannot_dodge_isolation(self):
        templates = {"assistant": ASSISTANT.replace('["{work}/{name}"]', '["{work}"]')}
        self.assertRejected(heteronym_file("""\
            pa: {template: assistant, folders: ["/work/x"]}
            pb: {template: assistant, folders: ["//work/x"]}
            """), "overlaps heteronyms.pb folder", templates)

    def test_template_root_slash(self):
        bad = ASSISTANT.replace('["{work}/{name}"]', '["/"]')
        self.assertRejected(heteronym_file("pa: {template: assistant}\n"),
                            "resolves to /", {"assistant": bad})

    def test_work_root_slash_and_braces(self):
        for root in ("/", "//work", "/w{name}"):
            with self.subTest(root=root):
                self.assertRejected(heteronym_file("pa: {template: assistant}\n"),
                                    "work root", work_root=root)

    def test_shared_chat(self):
        self.assertRejected(heteronym_file("""\
            pa: {template: assistant, channel: {kind: signal, chat: g1}}
            pb: {template: assistant, channel: {kind: signal, chat: g1}}
            """), "is already used by heteronyms.pa")

    def test_blank_chat(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, channel: {kind: signal, chat: ' '}}\n"),
                            "channel.chat: must be a non-empty string")

    def test_padded_chat(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, channel: {kind: signal, chat: ' g'}}\n"),
                            "no leading or trailing spaces")

    def test_empty_folders(self):
        self.assertRejected(heteronym_file("pa: {template: assistant, folders: []}\n"),
                            "must name at least one folder")

    def test_heteronyms_not_a_mapping(self):
        for value in ("[]", "false", "0"):
            with self.subTest(value=value):
                self.assertRejected(f"version: 1\nheteronyms: {value}\n",
                                    "heteronyms must be a mapping")

    def test_version_true_is_not_1(self):
        self.assertRejected("version: true\nheteronyms: {}\n", "version must be 1")

    def test_malformed_inputs_raise_config_error(self):
        cases = {
            "template is a list": heteronym_file("pa: {template: [assistant]}\n"),
            "non-scalar key": "version: 1\nheteronyms:\n  ? [a]\n  : 1\n",
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.assertRejected(body, "")

    def test_invalid_utf8_and_directory(self):
        ws = Workspace(heteronym_file("pa: {template: assistant}\n"))
        self.addCleanup(ws.close)
        ws.heteronyms.write_bytes(b"version: 1\n\xff\n")
        with self.assertRaises(ConfigError):
            ws.load()
        templates = load_templates(ws.root / "templates")
        with self.assertRaises(ConfigError):
            load_heteronyms(ws.root, templates)

    def test_folder_characters_tmux_would_interpret(self):
        # tmux expands #(cmd) in a start directory; ";" splits its commands.
        for folder in ("{work}/{name}/#(id)", "{work}/{name}/a;b", "{work}/{name}/a b",
                       "{work}/{name}/a\\nb"):
            with self.subTest(folder=folder):
                self.assertRejected(
                    heteronym_file(f"pa: {{template: assistant, folders: [\"{folder}\"]}}\n"),
                    "has characters outside")

    def test_work_root_unsafe_characters(self):
        self.assertRejected(heteronym_file("pa: {template: assistant}\n"),
                            "work root", work_root="/w#x")

    def test_all_errors_reported_together(self):
        ws = Workspace(heteronym_file("""\
            pa: {template: assistant, tier: frontier}
            pb: {template: assistant, permissions: [Bash]}
            """))
        self.addCleanup(ws.close)
        with self.assertRaises(ConfigError) as exc:
            ws.load()
        self.assertEqual(len(exc.exception.errors), 2)


class Helpers(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertIsNone(parse_duration("never"))
        for bad in ("0h", "2", "2w", "", None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_duration(bad)

    def test_resolve_folder_normalises(self):
        self.assertEqual(resolve_folder("{work}/{name}/", "/srv/w", "pa"), "/srv/w/pa")


if __name__ == "__main__":
    unittest.main()
