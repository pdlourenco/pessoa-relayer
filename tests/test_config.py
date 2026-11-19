"""Tests for personas.yaml and template validation (docs/SPEC.md §1, §2).

Every rejection rule has a case that shows it failing; the shipped examples
have a case that shows them passing.
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from relayer.config import (ConfigError, load_personas, load_templates,
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
    """A temp dir with a templates/ tree and a personas.yaml."""

    def __init__(self, personas: str, templates: dict[str, str] | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name, body in (templates or {"assistant": ASSISTANT}).items():
            d = self.root / "templates" / name
            d.mkdir(parents=True)
            (d / "template.yaml").write_text(textwrap.dedent(body))
        self.personas = self.root / "personas.yaml"
        self.personas.write_text(textwrap.dedent(personas))

    def load(self, work_root="/work"):
        templates = load_templates(self.root / "templates")
        return load_personas(self.personas, templates, work_root)

    def close(self):
        self._tmp.cleanup()


def persona_file(body: str) -> str:
    return "version: 1\npersonas:\n" + textwrap.indent(textwrap.dedent(body), "  ")


class ShippedExamples(unittest.TestCase):
    def test_examples_validate(self):
        templates = load_templates(REPO / "templates")
        personas = load_personas(REPO / "examples" / "personas.yaml", templates)
        self.assertIn("pa", personas)
        self.assertEqual(personas["pa"].folders, ("/work/pa",))


class ValidPersona(unittest.TestCase):
    def test_defaults_come_from_template(self):
        ws = Workspace(persona_file("pa:\n  template: assistant\n"))
        self.addCleanup(ws.close)
        p = ws.load()["pa"]
        self.assertEqual(p.tier, "standard")
        self.assertEqual(p.allow, ("Read", "Edit"))
        self.assertEqual(p.deny, ("Danger",))
        self.assertEqual(p.idle_stop, 7200)
        self.assertTrue(p.enabled)
        self.assertIsNone(p.channel)

    def test_narrower_permissions_and_subfolder_allowed(self):
        ws = Workspace(persona_file("""\
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

    def test_hash_changes_with_shape_not_with_enabled(self):
        a = Workspace(persona_file("pa: {template: assistant}\n"))
        b = Workspace(persona_file("pa: {template: assistant, enabled: false}\n"))
        c = Workspace(persona_file("pa: {template: assistant, tier: strong}\n"))
        for ws in (a, b, c):
            self.addCleanup(ws.close)
        ha, hb, hc = (ws.load()["pa"].config_hash() for ws in (a, b, c))
        self.assertEqual(ha, hb)
        self.assertNotEqual(ha, hc)


class Rejections(unittest.TestCase):
    """Each case must fail validation; the message names the rule."""

    def assertRejected(self, personas: str, expected: str, templates=None,
                       work_root="/work"):
        ws = Workspace(personas, templates)
        self.addCleanup(ws.close)
        with self.assertRaises(ConfigError) as exc:
            ws.load(work_root)
        joined = "\n".join(exc.exception.errors)
        self.assertIn(expected, joined)

    def test_tier_above_template_cap(self):
        self.assertRejected(persona_file("pa: {template: assistant, tier: frontier}\n"),
                            "exceeds template 'assistant' max_tier 'strong'")

    def test_unknown_tier(self):
        self.assertRejected(persona_file("pa: {template: assistant, tier: opus}\n"),
                            "'opus' is not one of")

    def test_permission_not_in_template(self):
        self.assertRejected(persona_file("pa: {template: assistant, permissions: [Bash]}\n"),
                            "'Bash' is not in template 'assistant' allow list")

    def test_folder_outside_template_root(self):
        self.assertRejected(persona_file('pa: {template: assistant, folders: ["{work}/other"]}\n'),
                            "is outside template 'assistant' roots")

    def test_folder_escape_with_dotdot(self):
        self.assertRejected(
            persona_file('pa: {template: assistant, folders: ["{work}/pa/../other"]}\n'),
            "'/work/other' is outside template")

    def test_relative_folder(self):
        self.assertRejected(persona_file('pa: {template: assistant, folders: ["pa"]}\n'),
                            "is not absolute")

    def test_unknown_placeholder(self):
        self.assertRejected(persona_file('pa: {template: assistant, folders: ["{home}/pa"]}\n'),
                            "unknown placeholder {home}")

    def test_overlapping_personas(self):
        templates = {"assistant": ASSISTANT.replace('["{work}/{name}"]', '["{work}"]')}
        self.assertRejected(persona_file("""\
            pa: {template: assistant, folders: ["{work}/shared"]}
            pb: {template: assistant, folders: ["{work}/shared/sub"]}
            """), "overlaps personas.pb folder", templates)

    def test_unknown_persona_key(self):
        self.assertRejected(persona_file("pa: {template: assistant, permision: [Read]}\n"),
                            "personas.pa.permision: unknown key")

    def test_unknown_template(self):
        self.assertRejected(persona_file("pa: {template: nope}\n"),
                            "unknown template 'nope'")

    def test_bad_name(self):
        self.assertRejected(persona_file("Bad_Name: {template: assistant}\n"),
                            "name must match")

    def test_duplicate_persona_key(self):
        self.assertRejected(persona_file("""\
            pa: {template: assistant}
            pa: {template: assistant, tier: fast}
            """), "duplicate key 'pa'")

    def test_channel_kind_not_allowed(self):
        self.assertRejected(
            persona_file("pa: {template: assistant, channel: {kind: telegram, chat: x}}\n"),
            "'telegram' not allowed by template 'assistant'")

    def test_channel_without_chat(self):
        self.assertRejected(persona_file("pa: {template: assistant, channel: {kind: signal}}\n"),
                            "channel.chat: must be a non-empty string")

    def test_bad_idle_stop(self):
        self.assertRejected(persona_file("pa: {template: assistant, idle_stop: 90}\n"),
                            "is not a duration")

    def test_wrong_version(self):
        self.assertRejected("version: 2\npersonas: {}\n", "version must be 1")

    def test_relative_work_root(self):
        self.assertRejected(persona_file("pa: {template: assistant}\n"),
                            "must be an absolute, normalised path", work_root="work")

    def test_template_default_above_max(self):
        bad = ASSISTANT.replace("default_tier: standard", "default_tier: frontier")
        self.assertRejected(persona_file("pa: {template: assistant}\n"),
                            "default_tier: 'frontier' exceeds max_tier 'strong'",
                            {"assistant": bad})

    def test_template_unknown_key(self):
        bad = ASSISTANT + "max_teir: fast\n"
        self.assertRejected(persona_file("pa: {template: assistant}\n"),
                            "template assistant.max_teir: unknown key",
                            {"assistant": bad})

    def test_all_errors_reported_together(self):
        ws = Workspace(persona_file("""\
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
