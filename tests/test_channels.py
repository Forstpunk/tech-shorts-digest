"""resolve-channels: parsing, the channels.yaml edit, and the confirm-everything flow."""

from pathlib import Path

import pytest
import yaml
from fakes import CH_A, CH_B, FakeYouTube, make_guard

from shorts_digest import store
from shorts_digest.channels import (
    MAX_SEARCHES,
    Resolver,
    add_channels_to_yaml,
    apply_with_confirmation,
    guess_handle,
    parse_line,
    read_names,
)
from shorts_digest.config import ChannelsConfig, ConfigError

CH_C = "UC" + "c" * 22
CH_D = "UC" + "d" * 22

YAML = f"""# my curated channels
topics:
  python:
    - {{id: {CH_A}, name: A}}
    # keep this comment
  data_engineering: []
  ai_llm:
    - {{id: {CH_B}, name: B}}
"""


# --- parsing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, kind, value",
    [
        (CH_C, "id", CH_C),
        (f"https://www.youtube.com/channel/{CH_C}", "id", CH_C),
        ("https://www.youtube.com/@TechWithTim/shorts", "handle", "@TechWithTim"),
        ("@fireship", "handle", "@fireship"),
        ("  Tech With Tim  ", "name", "Tech With Tim"),
    ],
)
def test_parse_line(line, kind, value):
    e = parse_line(line)
    assert (e.kind, e.value) == (kind, value)


def test_blank_and_comment_lines_ignored(tmp_path):
    f = tmp_path / "names.txt"
    f.write_text("# my list\n\nFireship\n   \n@b001io\n")
    assert [e.raw for e in read_names(f)] == ["Fireship", "@b001io"]


def test_missing_names_file_explains_format(tmp_path):
    with pytest.raises(ConfigError, match="one channel per line"):
        read_names(tmp_path / "nope.txt")


def test_guess_handle():
    assert guess_handle("Tech With Tim") == "@TechWithTim"
    assert guess_handle("Alex The Analyst!") == "@AlexTheAnalyst"


# --- editing channels.yaml ---------------------------------------------------------


def test_add_keeps_comments_and_appends_after_last_item():
    out = add_channels_to_yaml(YAML, [("python", CH_C, "Indently")])
    lines = out.splitlines()
    assert lines[0] == "# my curated channels"
    assert lines[3:6] == [
        f"    - {{id: {CH_A}, name: A}}",
        f"    - {{id: {CH_C}, name: Indently}}",
        "    # keep this comment",
    ]
    assert ChannelsConfig.model_validate(yaml.safe_load(out)).topics["python"][1].id == CH_C


def test_add_to_empty_topic_and_quote_awkward_names():
    out = add_channels_to_yaml(YAML, [("data_engineering", CH_C, "Data: the {real} deal, #1")])
    cfg = ChannelsConfig.model_validate(yaml.safe_load(out))
    assert cfg.topics["data_engineering"][0].name == "Data: the {real} deal, #1"
    assert "  data_engineering:\n    - {id:" in out


def test_unknown_topic_is_refused():
    with pytest.raises(ConfigError, match="not in channels.yaml"):
        add_channels_to_yaml(YAML, [("gaming", CH_C, "X")])


# --- the interactive flow ----------------------------------------------------------


class Script:
    """Feeds scripted answers to the prompts and records everything shown."""

    def __init__(self, *answers):
        self.answers, self.prompts, self.out = list(answers), [], []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def say(self, text):
        self.out.append(text)

    @property
    def text(self):
        return "\n".join(self.out + self.prompts)


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "c.db")


def config():
    return ChannelsConfig.model_validate(yaml.safe_load(YAML))


def make_yt():
    return FakeYouTube(
        info={
            CH_A: {"title": "A", "handle": "@a"},
            CH_C: {"title": "Tech With Tim", "handle": "@TechWithTim", "subs": 1_600_000},
            CH_D: {"title": "Tim Tech Reviews", "handle": "@timreviews", "hidden": True},
            "UC" + "e" * 22: {
                "title": "Coreyschafer",
                "handle": "@CoreySchafer",
                "subs": 68,
                "videos": 0,
            },
        },
        search={"Tim": [CH_D, CH_C]},
    )


def resolver(yt, conn, script, topic=None):
    guard = make_guard(conn, source="resolve")
    return Resolver(yt, guard, config(), script.ask, script.say, topic), guard


def test_handle_guess_hits_for_one_unit(conn):
    yt, s = make_yt(), Script("y", "1")  # right channel? yes; topic 1 = python
    r, guard = resolver(yt, conn, s)
    result = r.resolve([parse_line("Tech With Tim")])
    assert result.added == [("python", CH_C, "Tech With Tim")]
    assert "subscribers: 1,600,000" in s.text and "https://www.youtube.com/@TechWithTim" in s.text
    assert (guard.units, yt.count("search.list")) == (1, 0)


def test_guessed_handle_lookalike_is_flagged(conn):
    yt, s = make_yt(), Script("n", "n")  # not the right channel, no search
    r, _ = resolver(yt, conn, s)
    r.resolve([parse_line("Corey Schafer")])
    assert "possibly a look-alike" in s.text
    assert "only 68 subscribers" in s.text and "no videos" in s.text


def test_good_guess_has_no_warning(conn):
    yt, s = make_yt(), Script("y", "1")
    r, _ = resolver(yt, conn, s)
    r.resolve([parse_line("Tech With Tim")])
    assert "look-alike" not in s.text


def test_wrong_match_leads_to_search_only_after_yes(conn):
    yt = make_yt()
    # "@Tim" doesn't exist -> search? yes -> pick 2 (Tech With Tim) -> topic ai_llm
    s = Script("y", "2", "ai_llm")
    r, guard = resolver(yt, conn, s)
    result = r.resolve([parse_line("Tim")])
    assert result.added == [("ai_llm", CH_C, "Tech With Tim")]
    assert "subscribers: hidden" in s.text  # CH_D hides its count
    assert guard.units == 1 + 100 + 1  # handle guess, search, stats for the results
    assert result.searches == 1


def test_declining_search_spends_nothing_more(conn):
    yt, s = make_yt(), Script("n")
    r, guard = resolver(yt, conn, s)
    result = r.resolve([parse_line("Tim")])
    assert result.added == [] and result.skipped == [("Tim", "not resolved")]
    assert guard.units == 1


def test_search_is_capped_per_run(conn):
    yt = make_yt()
    entries = [parse_line(f"nobody{i}") for i in range(MAX_SEARCHES + 1)]
    s = Script(*(["y"] * (MAX_SEARCHES + 1)))  # yes to every search (none find anything)
    r, _ = resolver(yt, conn, s)
    r.resolve(entries)
    assert yt.count("search.list") == MAX_SEARCHES
    assert "Search limit reached" in s.text


def test_already_listed_channel_is_skipped_without_questions(conn):
    yt, s = make_yt(), Script()
    r, _ = resolver(yt, conn, s)
    result = r.resolve([parse_line("@a")])
    assert result.skipped == [("@a", "already in python")]
    assert s.prompts == []


def test_same_channel_twice_in_one_file_added_once(conn):
    yt, s = make_yt(), Script("y", "1")
    r, _ = resolver(yt, conn, s)
    result = r.resolve([parse_line("@TechWithTim"), parse_line(CH_C)])
    assert len(result.added) == 1
    assert result.skipped == [(CH_C, "already in python")]


def test_default_topic_used_on_enter(conn):
    yt, s = make_yt(), Script("y", "")
    r, _ = resolver(yt, conn, s, topic="ai_llm")
    assert r.resolve([parse_line("@TechWithTim")]).added[0][0] == "ai_llm"


def test_unknown_id_is_skipped_without_search(conn):
    yt, s = make_yt(), Script()
    r, _ = resolver(yt, conn, s)
    result = r.resolve([parse_line("UC" + "z" * 22)])
    assert result.skipped[0][1] == "not resolved"
    assert yt.count("search.list") == 0


# --- writing -----------------------------------------------------------------------


def test_diff_shown_and_written_only_on_yes(tmp_path):
    path = tmp_path / "channels.yaml"
    path.write_text(YAML)
    add = [("python", CH_C, "Tech With Tim")]

    no = Script("n")
    assert apply_with_confirmation(path, add, no.ask, no.say) is False
    assert path.read_text() == YAML
    assert f"+    - {{id: {CH_C}, name: Tech With Tim}}" in no.text

    yes = Script("y")
    assert apply_with_confirmation(path, add, yes.ask, yes.say) is True
    assert CH_C in path.read_text() and "# keep this comment" in path.read_text()


def test_nothing_to_add_never_asks(tmp_path):
    path = tmp_path / "channels.yaml"
    path.write_text(YAML)
    s = Script()
    assert apply_with_confirmation(path, [], s.ask, s.say) is False
    assert s.prompts == []


# --- CLI end to end ----------------------------------------------------------------


def test_cli_resolve_channels_end_to_end(monkeypatch, tmp_path, capsys):
    import shorts_digest.cli as cli
    import shorts_digest.config as config_mod
    import shorts_digest.youtube as youtube
    from shorts_digest.config import load_config

    (tmp_path / "settings.yaml").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "settings.yaml").read_text()
    )
    (tmp_path / "channels.yaml").write_text(YAML)
    names = tmp_path / "names.txt"
    names.write_text("Tech With Tim\n@a\n")

    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(cli, "load_config", lambda: load_config(tmp_path, tmp_path / "none.env"))
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(youtube, "build_client", lambda key: make_yt())
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    answers = iter(["y", "2", "y"])  # right channel, topic 2 = data_engineering, write
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert cli.main(["resolve-channels", str(names)]) == 0
    saved = ChannelsConfig.model_validate(yaml.safe_load((tmp_path / "channels.yaml").read_text()))
    assert [c.id for c in saved.topics["data_engineering"]] == [CH_C]
    out = capsys.readouterr().out
    assert "skipped '@a': already in python" in out
    usage = store.quota_breakdown(store.connect(tmp_path / "cli.db"), make_guard_day())
    assert [(r["source"], r["units"]) for r in usage] == [("resolve", 2)]


def make_guard_day():
    from shorts_digest.quota import quota_day

    return quota_day()
