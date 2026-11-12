"""Curated channel list management.

Phase 7: ``resolve-channels`` turns channel names into verified channel IDs and adds them
to config/channels.yaml - but only after the owner confirms each match and the final diff.
(Phase 9 adds the promotion flow for discovered channels here.)

Lookup order per line, cheapest first:
1. a ``UC...`` ID or /channel/ URL  -> channels.list by id        (1 unit)
2. an @handle or /@handle URL       -> channels.list forHandle    (1 unit)
3. a plain name                     -> guess the handle (spaces removed), 1 unit
4. still nothing, or wrong match    -> channel search, only if the owner says yes (100 units,
                                       at most MAX_SEARCHES per invocation)

channels.yaml is edited as text (not re-dumped through PyYAML) so its comments and layout
survive. The edited text is validated before anything is written.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from shorts_digest.config import ChannelsConfig, ConfigError
from shorts_digest.youtube import (
    ChannelInfo,
    Quota,
    channel_by_handle,
    channels_by_id,
    search_channels,
)

MAX_SEARCHES = 10  # search.list costs 100 units; CLAUDE.md caps it at 10 per run
SEARCH_RESULTS = 5

_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
_URL_ID_RE = re.compile(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})")
_URL_HANDLE_RE = re.compile(r"youtube\.com/(@[\w.\-]+)")

Ask = Callable[[str], str]
Say = Callable[[str], None]


# --- reading the names file --------------------------------------------------------


@dataclass
class NameEntry:
    raw: str
    kind: str  # "id" | "handle" | "name"
    value: str


def parse_line(line: str) -> NameEntry | None:
    """Classify one line of channel_names.txt. Blank lines and # comments return None."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if m := _URL_ID_RE.search(raw):
        return NameEntry(raw, "id", m.group(1))
    if _ID_RE.fullmatch(raw):
        return NameEntry(raw, "id", raw)
    if m := _URL_HANDLE_RE.search(raw):
        return NameEntry(raw, "handle", m.group(1))
    if raw.startswith("@"):
        return NameEntry(raw, "handle", raw)
    return NameEntry(raw, "name", raw)


def read_names(path: Path) -> list[NameEntry]:
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Create it with one channel per line: a name "
            "(Tech With Tim), an @handle, a channel URL or a UC... channel ID."
        )
    entries = [parse_line(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [e for e in entries if e]


def guess_handle(name: str) -> str:
    """'Tech With Tim' -> '@TechWithTim'. Handles can't contain spaces; many channels just
    squash their name, so this one-unit guess often hits before we need a 100-unit search."""
    return "@" + re.sub(r"[^\w.\-]", "", name)


# --- editing channels.yaml ---------------------------------------------------------


def _yaml_name(name: str) -> str:
    """Write the name bare if YAML reads it back unchanged, else as a quoted string."""
    try:
        if yaml.safe_load(f"{{name: {name}}}") == {"name": name}:
            return name
    except yaml.YAMLError:
        pass
    return json.dumps(name, ensure_ascii=False)  # a JSON string is valid YAML


def add_channels_to_yaml(text: str, additions: list[tuple[str, str, str]]) -> str:
    """Insert (topic, channel_id, name) entries at the end of each topic's list.

    Only existing topics are allowed (they drive the extractor's topic list).
    """
    lines = text.splitlines()
    for topic, cid, name in additions:
        entry = f"    - {{id: {cid}, name: {_yaml_name(name)}}}"
        start = next(
            (i for i, ln in enumerate(lines) if re.fullmatch(rf"  {re.escape(topic)}:.*", ln)),
            None,
        )
        if start is None:
            raise ConfigError(f"topic {topic!r} is not in channels.yaml")
        if lines[start].rstrip().endswith("[]"):  # "  python: []" -> block list
            lines[start] = f"  {topic}:"
        end = start + 1
        while end < len(lines) and lines[end].startswith("    "):
            end += 1
        # Put the entry after the last list item, not after trailing comments/blank lines.
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].lstrip().startswith("- "):
            insert_at -= 1
        lines.insert(insert_at, entry)
    return "\n".join(lines) + "\n"


def validate_channels_text(text: str) -> ChannelsConfig:
    try:
        return ChannelsConfig.model_validate(yaml.safe_load(text))
    except Exception as exc:  # YAML error or pydantic ValidationError
        raise ConfigError(f"edited channels.yaml would be invalid: {exc}") from exc


# --- the interactive resolver ------------------------------------------------------


def describe(ch: ChannelInfo) -> str:
    subs = "hidden" if ch.subscribers is None else f"{ch.subscribers:,}"
    videos = "?" if ch.video_count is None else f"{ch.video_count:,}"
    return f"{ch.title}  ({ch.id})  subscribers: {subs}, videos: {videos}  {ch.url}"


SUSPICIOUS_SUBSCRIBERS = 1000  # an "established channel" below this is probably a look-alike


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def lookalike_warning(name: str, ch: ChannelInfo) -> str | None:
    """Why a channel found by *guessing* the handle from a name may be the wrong one.

    Handles are first-come-first-served, so "@CoreySchafer" can belong to a squatter while the
    real channel is "@coreyms". Found on real data during phase 7.
    """
    problems = []
    if ch.video_count == 0:
        problems.append("no videos")
    if ch.subscribers is not None and ch.subscribers < SUSPICIOUS_SUBSCRIBERS:
        problems.append(f"only {ch.subscribers} subscribers")
    if _squash(ch.title) != _squash(name):
        problems.append(f"title {ch.title!r} differs from {name!r}")
    if not problems:
        return None
    return "  Warning: handle was guessed and " + ", ".join(problems) + " - possibly a look-alike."


def _yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes")


@dataclass
class ResolveResult:
    added: list[tuple[str, str, str]]  # (topic, id, name)
    skipped: list[tuple[str, str]]  # (raw line, why)
    searches: int = 0
    written: bool = False


class Resolver:
    def __init__(
        self,
        client: Any,
        quota: Quota,
        config: ChannelsConfig,
        ask: Ask,
        say: Say,
        default_topic: str | None = None,
    ):
        self.client, self.quota, self.ask, self.say = client, quota, ask, say
        self.topics = config.topic_names
        self.known = {ch.id: topic for topic, ch in config.all_channels()}
        self.default_topic = default_topic
        self.searches = 0

    def _lookup(self, entry: NameEntry) -> ChannelInfo | None:
        if entry.kind == "id":
            return channels_by_id(self.client, [entry.value], self.quota).get(entry.value)
        handle = entry.value if entry.kind == "handle" else guess_handle(entry.value)
        return channel_by_handle(self.client, handle, self.quota)

    def _search(self, query: str) -> ChannelInfo | None:
        left = MAX_SEARCHES - self.searches
        if left <= 0:
            self.say(f"  Search limit reached ({MAX_SEARCHES} per run).")
            return None
        prompt = f'  Search YouTube for "{query}"? Costs 100 units ({left} left) [y/N] '
        if not _yes(self.ask(prompt)):
            return None
        self.searches += 1
        matches = search_channels(self.client, query, self.quota, SEARCH_RESULTS)
        if not matches:
            self.say("  No channels found.")
            return None
        for i, ch in enumerate(matches, 1):
            self.say(f"   {i}) {describe(ch)}")
        pick = self.ask(f"  Pick 1-{len(matches)}, or Enter to skip: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(matches):
            return matches[int(pick) - 1]
        return None

    def _topic(self) -> str | None:
        menu = "  ".join(f"{i}) {t}" for i, t in enumerate(self.topics, 1))
        hint = f", Enter = {self.default_topic}" if self.default_topic else ", Enter = skip"
        answer = self.ask(f"  Topic? {menu}{hint}: ").strip()
        if not answer:
            return self.default_topic
        if answer.isdigit() and 1 <= int(answer) <= len(self.topics):
            return self.topics[int(answer) - 1]
        if answer in self.topics:
            return answer
        self.say("  Not a topic; skipped.")
        return None

    def resolve(self, entries: list[NameEntry]) -> ResolveResult:
        result = ResolveResult(added=[], skipped=[])
        for n, entry in enumerate(entries, 1):
            self.say(f"[{n}/{len(entries)}] {entry.raw}")
            ch = self._lookup(entry)
            if ch is not None:
                self.say(f"  Found: {describe(ch)}")
                if entry.kind == "name" and (warning := lookalike_warning(entry.value, ch)):
                    self.say(warning)
                # Already-listed channels skip the question; they're reported below.
                if ch.id not in self.known and not _yes(self.ask("  Right channel? [y/N] ")):
                    ch = None
            else:
                self.say("  No channel with that handle.")
            if ch is None and entry.kind != "id":
                ch = self._search(entry.raw.lstrip("@"))
            if ch is None:
                result.skipped.append((entry.raw, "not resolved"))
                continue
            if ch.id in self.known:
                self.say(f"  Already listed under {self.known[ch.id]}; skipped.")
                result.skipped.append((entry.raw, f"already in {self.known[ch.id]}"))
                continue
            topic = self._topic()
            if topic is None:
                result.skipped.append((entry.raw, "no topic chosen"))
                continue
            result.added.append((topic, ch.id, ch.title))
            self.known[ch.id] = topic
        result.searches = self.searches
        return result


def apply_with_confirmation(
    path: Path, additions: list[tuple[str, str, str]], ask: Ask, say: Say
) -> bool:
    """Show the channels.yaml diff, write only on an explicit yes."""
    if not additions:
        say("Nothing to add; channels.yaml unchanged.")
        return False
    old = path.read_text(encoding="utf-8")
    new = add_channels_to_yaml(old, additions)
    validate_channels_text(new)
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(), str(path.name), str(path.name), lineterm=""
    )
    say("\n".join(diff))
    if not _yes(ask(f"Write {len(additions)} channel(s) to {path.name}? [y/N] ")):
        say("Not written.")
        return False
    path.write_text(new, encoding="utf-8")
    say(f"Saved. {len(additions)} channel(s) added.")
    return True
