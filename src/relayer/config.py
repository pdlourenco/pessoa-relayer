"""Load and validate ``heteronyms.yaml`` and heteronym templates.

The format is specified in docs/SPEC.md §1 (heteronyms) and §2 (templates).
Validation collects every error rather than stopping at the first, so one
run of ``relayer validate`` shows everything wrong with a file.

Security-relevant rules (docs/SPEC.md §2.2): a heteronym can never get a tier,
a permission or a folder its template does not allow. Unknown keys are
errors, because a misspelt key that is silently ignored can drop a cap.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .tiers import TIERS, rank

FORMAT_VERSION = 1
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# Resolved folders and the work root use this character set only. tmux
# expands formats such as #(cmd) in a session's start directory, and a
# trailing ";" splits a tmux command line; neither character can appear.
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._@+/-]*$")
DURATION_RE = re.compile(r"^([1-9][0-9]*)([mhd])$")
DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}

HETERONYM_KEYS = {"class", "template", "tier", "channel", "folders",
                  "permissions", "idle_stop", "enabled"}
TEMPLATE_KEYS = {"version", "description", "classes", "max_tier", "default_tier",
                 "folders", "permissions", "channel_kinds", "idle_stop",
                 "chat_creatable"}
CHANNEL_KEYS = {"kind", "chat"}
PERMISSION_KEYS = {"allow", "deny"}


class ConfigError(Exception):
    """One or more validation errors; ``errors`` lists them all."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    classes: tuple[str, ...]        # trust classes whose heteronyms may use it
    max_tier: str
    default_tier: str
    folders: tuple[str, ...]        # roots, unresolved ({work}, {name})
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    channel_kinds: tuple[str, ...]
    idle_stop: int | None           # seconds; None = never
    chat_creatable: bool


@dataclass(frozen=True)
class Channel:
    kind: str
    chat: str


@dataclass(frozen=True)
class Heteronym:
    """A heteronym with template defaults applied and placeholders resolved."""

    name: str
    klass: str                      # trust class ("class" in the file)
    template: str
    tier: str
    channel: Channel | None
    folders: tuple[str, ...]        # absolute, normalised
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    idle_stop: int | None
    enabled: bool

    def config_hash(self) -> str:
        """Stable digest of everything that shapes the running session.

        The reconciler restarts a session whose hash changed. ``enabled``
        and ``idle_stop`` are excluded: they decide *whether* a session
        runs, not how it is configured.
        """
        shape = {
            "class": self.klass,
            "template": self.template,
            "tier": self.tier,
            "channel": None if self.channel is None else
            {"kind": self.channel.kind, "chat": self.channel.chat},
            "folders": list(self.folders),
            "allow": list(self.allow),
            "deny": list(self.deny),
        }
        blob = json.dumps(shape, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --- YAML loading -----------------------------------------------------------

class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys.

    PyYAML keeps the last of two equal keys without a word, so a second
    ``pa:`` entry would silently replace the first.
    """


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float)) or isinstance(key, bool):
            raise yaml.constructor.ConstructorError(
                None, None, "mapping keys must be strings or numbers", key_node.start_mark)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _load_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.load(fh, Loader=_StrictLoader)  # noqa: S506 (strict safe loader)
    except FileNotFoundError:
        raise ConfigError([f"{path}: file not found"]) from None
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError([f"{path}: {exc}"]) from None


# --- small validators ---------------------------------------------------------

def parse_duration(value: Any) -> int | None:
    """``"30m"``, ``"2h"``, ``"1d"`` → seconds; ``"never"`` → None."""
    if value == "never":
        return None
    if isinstance(value, str):
        m = DURATION_RE.match(value)
        if m:
            return int(m.group(1)) * DURATION_UNITS[m.group(2)]
    raise ValueError(f"{value!r} is not a duration like 30m, 2h, 1d or 'never'")


def _is_version(value: Any) -> bool:
    # bool is an int subclass: `version: true` must not pass as 1.
    return type(value) is int and value == FORMAT_VERSION


def _str_list(value: Any, where: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where}: must be a list of strings")
        return ()
    return tuple(value)


def _unknown_keys(data: dict, allowed: set[str], where: str,
                  errors: list[str]) -> None:
    for key in sorted(set(data) - allowed, key=str):
        errors.append(f"{where}.{key}: unknown key")


def _tier(value: Any, where: str, errors: list[str]) -> str | None:
    if value not in TIERS:
        errors.append(f"{where}: {value!r} is not one of {', '.join(TIERS)}")
        return None
    return value


def resolve_folder(pattern: str, work_root: str, name: str) -> str:
    """Substitute placeholders and normalise; raises ValueError if unsafe."""
    for placeholder in re.findall(r"\{([^}]*)\}", pattern):
        if placeholder not in ("work", "name"):
            raise ValueError(f"unknown placeholder {{{placeholder}}}")
    path = pattern.replace("{work}", work_root).replace("{name}", name)
    if not path.startswith("/"):
        raise ValueError(f"{path!r} is not absolute")
    # normpath keeps a leading "//" (POSIX leaves it implementation-defined);
    # on Linux it is the same directory as "/", so collapse it first.
    path = posixpath.normpath("/" + path.lstrip("/"))
    if path == "/":
        raise ValueError("resolves to /, which would contain every folder")
    if not SAFE_PATH_RE.match(path):
        raise ValueError(f"{path!r} has characters outside [A-Za-z0-9._@+-/]")
    return path


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


# --- templates ------------------------------------------------------------------

def load_template(path: Path) -> Template:
    """Load ``templates/<name>/template.yaml``; the directory names it."""
    name = path.parent.name
    data = _load_yaml(path)
    errors: list[str] = []
    where = f"template {name}"
    if not isinstance(data, dict):
        raise ConfigError([f"{where}: must be a mapping"])
    _unknown_keys(data, TEMPLATE_KEYS, where, errors)
    if not _is_version(data.get("version")):
        errors.append(f"{where}.version: must be {FORMAT_VERSION}")
    if not NAME_RE.match(name):
        errors.append(f"{where}: directory name must match {NAME_RE.pattern}")

    classes = _str_list(data.get("classes"), f"{where}.classes", errors)
    if "classes" in data and not classes and isinstance(data.get("classes"), list):
        errors.append(f"{where}.classes: must name at least one trust class")
    for cls in classes:
        if not NAME_RE.match(cls):
            errors.append(f"{where}.classes: {cls!r} must match {NAME_RE.pattern}")

    max_tier = _tier(data.get("max_tier"), f"{where}.max_tier", errors)
    default_tier = _tier(data.get("default_tier"), f"{where}.default_tier", errors)
    if max_tier and default_tier and rank(default_tier) > rank(max_tier):
        errors.append(f"{where}.default_tier: {default_tier!r} exceeds "
                      f"max_tier {max_tier!r}")

    folders = _str_list(data.get("folders"), f"{where}.folders", errors)
    if not folders and "folders" in data:
        errors.append(f"{where}.folders: must name at least one root")
    for folder in folders:
        try:
            resolve_folder(folder, "/w", "n")
        except ValueError as exc:
            errors.append(f"{where}.folders: {folder!r}: {exc}")

    perms = data.get("permissions", {})
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    if not isinstance(perms, dict):
        errors.append(f"{where}.permissions: must be a mapping")
    else:
        _unknown_keys(perms, PERMISSION_KEYS, f"{where}.permissions", errors)
        allow = _str_list(perms.get("allow", []), f"{where}.permissions.allow", errors)
        deny = _str_list(perms.get("deny", []), f"{where}.permissions.deny", errors)

    kinds = _str_list(data.get("channel_kinds", []), f"{where}.channel_kinds", errors)

    idle_stop: int | None = None
    try:
        idle_stop = parse_duration(data.get("idle_stop", "never"))
    except ValueError as exc:
        errors.append(f"{where}.idle_stop: {exc}")

    chat_creatable = data.get("chat_creatable", False)
    if not isinstance(chat_creatable, bool):
        errors.append(f"{where}.chat_creatable: must be true or false")

    description = data.get("description", "")
    if not isinstance(description, str):
        errors.append(f"{where}.description: must be a string")

    if errors:
        raise ConfigError(errors)
    return Template(name=name, description=description, classes=classes,
                    max_tier=max_tier,
                    default_tier=default_tier, folders=folders, allow=allow,
                    deny=deny, channel_kinds=kinds, idle_stop=idle_stop,
                    chat_creatable=chat_creatable)


def load_templates(directory: Path) -> dict[str, Template]:
    templates: dict[str, Template] = {}
    errors: list[str] = []
    for path in sorted(directory.glob("*/template.yaml")):
        try:
            tpl = load_template(path)
            templates[tpl.name] = tpl
        except ConfigError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ConfigError(errors)
    return templates


# --- heteronyms -------------------------------------------------------------------

def _heteronym(name: str, data: Any, templates: dict[str, Template],
               work_root: str, errors: list[str]) -> Heteronym | None:
    where = f"heteronyms.{name}"
    start = len(errors)
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(f"{where}: name must match {NAME_RE.pattern}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    _unknown_keys(data, HETERONYM_KEYS, where, errors)

    klass = data.get("class")
    if not isinstance(klass, str) or not NAME_RE.match(klass):
        errors.append(f"{where}.class: required; must match {NAME_RE.pattern}")
        return None

    tpl_name = data.get("template")
    tpl = templates.get(tpl_name) if isinstance(tpl_name, str) else None
    if tpl is None:
        errors.append(f"{where}.template: unknown template {data.get('template')!r}")
        return None
    if klass not in tpl.classes:
        errors.append(f"{where}.class: template {tpl.name!r} is not for class "
                      f"{klass!r} (it allows {list(tpl.classes)})")

    tier = _tier(data.get("tier", tpl.default_tier), f"{where}.tier", errors)
    if tier and rank(tier) > rank(tpl.max_tier):
        errors.append(f"{where}.tier: {tier!r} exceeds template {tpl.name!r} "
                      f"max_tier {tpl.max_tier!r}")

    channel = None
    if data.get("channel") is not None:
        ch = data["channel"]
        if not isinstance(ch, dict):
            errors.append(f"{where}.channel: must be a mapping")
        else:
            _unknown_keys(ch, CHANNEL_KEYS, f"{where}.channel", errors)
            kind, chat = ch.get("kind"), ch.get("chat")
            if kind not in tpl.channel_kinds:
                errors.append(f"{where}.channel.kind: {kind!r} not allowed by "
                              f"template {tpl.name!r} ({list(tpl.channel_kinds)})")
            if not isinstance(chat, str) or not chat.strip():
                errors.append(f"{where}.channel.chat: must be a non-empty string")
            elif chat != chat.strip():
                errors.append(f"{where}.channel.chat: no leading or trailing spaces")
            if isinstance(kind, str) and isinstance(chat, str):
                channel = Channel(kind=kind, chat=chat)

    roots = []
    for pattern in tpl.folders:
        roots.append(resolve_folder(pattern, work_root, name))
    folders: list[str] = []
    patterns = _str_list(data.get("folders", list(tpl.folders)),
                         f"{where}.folders", errors)
    if "folders" in data and isinstance(data["folders"], list) and not data["folders"]:
        errors.append(f"{where}.folders: must name at least one folder")
    for pattern in patterns:
        try:
            path = resolve_folder(pattern, work_root, name)
        except ValueError as exc:
            errors.append(f"{where}.folders: {pattern!r}: {exc}")
            continue
        if not any(_within(path, root) for root in roots):
            errors.append(f"{where}.folders: {path!r} is outside template "
                          f"{tpl.name!r} roots {roots}")
        folders.append(path)

    allow = tpl.allow
    if "permissions" in data:
        requested = _str_list(data["permissions"], f"{where}.permissions", errors)
        for perm in requested:
            if perm not in tpl.allow:
                errors.append(f"{where}.permissions: {perm!r} is not in template "
                              f"{tpl.name!r} allow list")
        allow = requested

    idle_stop = tpl.idle_stop
    if "idle_stop" in data:
        try:
            idle_stop = parse_duration(data["idle_stop"])
        except ValueError as exc:
            errors.append(f"{where}.idle_stop: {exc}")

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append(f"{where}.enabled: must be true or false")

    if len(errors) > start:
        return None
    return Heteronym(name=name, klass=klass, template=tpl.name, tier=tier,
                     channel=channel, folders=tuple(folders), allow=tuple(allow),
                     deny=tpl.deny, idle_stop=idle_stop, enabled=enabled)


def _overlaps(heteronyms: dict[str, Heteronym]) -> list[str]:
    """No shared or nested folders within a class, no shared chat at all
    (docs/SPEC.md §1.3). Classes run in separate containers with separate
    work roots, so folders only clash inside one class."""
    errors = []
    items = sorted(heteronyms.items())
    chats: dict[tuple[str, str], str] = {}
    for name, p in items:
        if p.channel is None:
            continue
        key = (p.channel.kind, p.channel.chat)
        if key in chats:
            errors.append(f"heteronyms.{name}.channel: {p.channel.kind} chat "
                          f"{p.channel.chat!r} is already used by heteronyms.{chats[key]}")
        else:
            chats[key] = name
    for i, (a_name, a) in enumerate(items):
        for b_name, b in items[i + 1:]:
            if a.klass != b.klass:
                continue
            for fa in a.folders:
                for fb in b.folders:
                    if _within(fa, fb) or _within(fb, fa):
                        errors.append(f"heteronyms.{a_name}.folders: {fa!r} overlaps "
                                      f"heteronyms.{b_name} folder {fb!r}")
    return errors


def load_heteronyms(path: Path, templates: dict[str, Template],
                    work_root: str = "/work",
                    klass: str | None = None) -> dict[str, Heteronym]:
    """Load and validate ``heteronyms.yaml``; raises ConfigError on any error.

    With ``klass``, only that trust class's entries are validated and
    returned (docs/SPEC.md §1.5): each container runs one class, and an
    error in another class's entry is that container's to report.
    """
    data = _load_yaml(path)
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ConfigError([f"{path}: must be a mapping"])
    _unknown_keys(data, {"version", "heteronyms"}, str(path), errors)
    if not _is_version(data.get("version")):
        errors.append(f"{path}: version must be {FORMAT_VERSION}")
    entries = data.get("heteronyms")
    if entries is None:
        entries = {}
    if not isinstance(entries, dict):
        raise ConfigError(errors + [f"{path}: heteronyms must be a mapping"])

    if (not work_root.startswith("/") or work_root.startswith("//")
            or posixpath.normpath(work_root) != work_root or work_root == "/"
            or not SAFE_PATH_RE.match(work_root)):
        raise ConfigError(errors + [f"work root {work_root!r} must be an "
                                    "absolute, normalised path below /, "
                                    "using only [A-Za-z0-9._@+-/]"])

    heteronyms: dict[str, Heteronym] = {}
    for name, entry in entries.items():
        if (klass is not None and isinstance(entry, dict)
                and isinstance(entry.get("class"), str) and entry["class"] != klass):
            continue
        heteronym = _heteronym(name, entry, templates, work_root, errors)
        if heteronym is not None:
            heteronyms[name] = heteronym
    errors.extend(_overlaps(heteronyms))
    if errors:
        raise ConfigError(errors)
    return heteronyms
