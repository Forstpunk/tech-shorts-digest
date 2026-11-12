"""Email (fake SMTP server, no network) and the GitHub Actions workflow file."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fakes import CH_A, FakeYouTube, settings_text

import shorts_digest.notify as notify
from shorts_digest import store
from shorts_digest.config import ENV_KEYS, ConfigError, Env, load_config
from shorts_digest.notify import build_message, markdown_to_html, send_digest

ROOT = Path(__file__).resolve().parents[1]
SMTP_ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "me@example.com",
    "SMTP_APP_PASSWORD": "fake-app-password",
    "DIGEST_TO": "me@example.com",
}


class FakeSMTP:
    """Records what the code does; can be told to fail on login."""

    sent: list = []
    events: list = []
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.events.append(("connect", type(self).__name__, host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSMTP.events.append(("starttls",))

    def login(self, user, password):
        if FakeSMTP.fail_login:
            import smtplib

            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        FakeSMTP.events.append(("login", user))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.events, FakeSMTP.fail_login = [], [], False
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


# --- message -----------------------------------------------------------------------


def test_markdown_to_html_formats_and_escapes():
    md = "# Title\n## AI / LLM\n1. **<b>Bold</b> idea** — see https://x.y/z  \n_note_ `cmd`"
    out = markdown_to_html(md)
    assert "<h2>Title</h2>" in out and "<h3>AI / LLM</h3>" in out
    assert "<b>&lt;b&gt;Bold&lt;/b&gt; idea</b>" in out  # titles can't inject HTML
    assert '<a href="https://x.y/z">https://x.y/z</a>' in out
    assert "<i>note</i>" in out and "<code>cmd</code>" in out


def test_message_has_text_html_and_csv(tmp_path):
    csv_path = tmp_path / "digest.csv"
    csv_path.write_text("rank,concept\n1,x\n")
    msg = build_message("Subj", "# Hi", "me@example.com", "you@example.com", csv_path)
    types = [part.get_content_type() for part in msg.walk()]
    assert "text/plain" in types and "text/html" in types and "text/csv" in types
    assert msg["To"] == "you@example.com"


# --- sending -----------------------------------------------------------------------


def test_send_uses_starttls_on_587(fake_smtp):
    send_digest(Env(SMTP_ENV), "Subj", "# Hi")
    assert fake_smtp.events == [
        ("connect", "FakeSMTP", "smtp.example.com", 587),
        ("starttls",),
        ("login", "me@example.com"),
    ]
    assert fake_smtp.sent[0]["Subject"] == "Subj"


def test_send_uses_ssl_on_465(fake_smtp):
    send_digest(Env(SMTP_ENV | {"SMTP_PORT": "465"}), "Subj", "# Hi")
    assert fake_smtp.events[0][1] == "FakeSMTPSSL"
    assert ("starttls",) not in fake_smtp.events


def test_missing_smtp_key_is_a_clear_error():
    with pytest.raises(ConfigError, match="SMTP_APP_PASSWORD"):
        send_digest(Env(SMTP_ENV | {"SMTP_APP_PASSWORD": ""}), "Subj", "# Hi")


# --- inside `run` ------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    import shorts_digest.cli as cli
    import shorts_digest.digest as digest
    import shorts_digest.youtube as youtube

    (tmp_path / "settings.yaml").write_text(settings_text(email=True))
    (tmp_path / "channels.yaml").write_text(f"topics:\n  python:\n    - {{id: {CH_A}, name: A}}\n")
    for k, v in SMTP_ENV.items() | {"YOUTUBE_API_KEY": "fake-key-for-tests"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli, "load_config", lambda: load_config(tmp_path, tmp_path / "none.env"))
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", tmp_path / "run.db")
    monkeypatch.setattr(digest, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    yt = FakeYouTube(channels={CH_A: "UUa"}, playlists={"UUa": []})
    monkeypatch.setattr(youtube, "build_client", lambda key: yt)
    return cli, yt


def seed_accepted(n=2):
    """Accepted curated items ready for a digest (as if earlier stages had run)."""
    conn = store.connect()
    store.upsert_channel(conn, CH_A, "A", "python")
    published = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(n):
        vid = f"v{i}"
        store.insert_videos(conn, [store.VideoRow(vid, CH_A, f"T{i}", "", published, 40, True)])
        store.save_text(conn, vid, "transcript", "t", "en", None)
        store.save_extraction(
            conn,
            vid,
            dict(
                concept=f"{vid * 4} idea",
                method_or_tool=None,
                topic="python",
                summary="S.",
                difficulty="beginner",
                usefulness=4,
                is_promotional=False,
                confidence="high",
                next_step=None,
                reasons=[],
            ),
            "rules",
            "test",
        )
        store.set_status(conn, vid, "accepted")
    return conn


def test_run_emails_the_digest(cli_env, fake_smtp, caplog):
    cli, _ = cli_env
    caplog.set_level("INFO")
    seed_accepted()
    assert cli.main(["run", "--no-discovery"]) == 0
    msg = fake_smtp.sent[0]
    assert re.match(r"Tech Shorts Digest — \d+ \w+ \d{4}: 2 picks$", msg["Subject"])
    assert "email=sent" in caplog.text


def test_failed_email_fails_the_run_and_keeps_items_unsent(cli_env, fake_smtp, caplog):
    cli, _ = cli_env
    seed_accepted()
    fake_smtp.fail_login = True
    assert cli.main(["run", "--no-discovery"]) == 1
    conn = store.connect()
    run = conn.execute("SELECT status, stage, error FROM runs").fetchone()
    assert (run["status"], run["stage"]) == ("failed", "notify")
    assert "SMTPAuthenticationError" in run["error"]
    assert "fake-app-password" not in run["error"] and "fake-app-password" not in caplog.text
    assert conn.execute("SELECT count(*) FROM digest_items").fetchone()[0] == 0  # resend later

    fake_smtp.fail_login = False  # next day, password fixed: the same items go out
    assert cli.main(["run", "--no-discovery"]) == 0
    assert "2 picks" in fake_smtp.sent[0]["Subject"]


def test_no_email_on_an_empty_day(cli_env, fake_smtp, caplog):
    cli, _ = cli_env
    caplog.set_level("INFO")
    assert cli.main(["run", "--no-discovery"]) == 0
    assert fake_smtp.sent == [] and "email=skipped_empty" in caplog.text


def test_missing_smtp_keys_stop_before_any_api_call(cli_env, monkeypatch, caplog):
    cli, yt = cli_env
    monkeypatch.delenv("DIGEST_TO")
    assert cli.main(["run"]) == 2
    assert "DIGEST_TO" in caplog.text
    assert yt.calls == []


def test_email_test_command_sends_newest_digest(cli_env, fake_smtp):
    cli, _ = cli_env
    assert cli.main(["digest"]) == 0
    assert cli.main(["email-test"]) == 0
    assert "test email" in fake_smtp.sent[0]["Subject"]


# --- the workflow file -------------------------------------------------------------

WORKFLOW = ROOT / ".github" / "workflows" / "daily.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_schedule_and_manual_trigger(workflow):
    triggers = workflow[True]  # YAML 1.1 reads the key `on` as True
    assert triggers["schedule"] == [{"cron": "30 1 * * *"}]  # 07:00 IST
    assert "workflow_dispatch" in triggers
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_workflow_takes_every_secret_from_repository_secrets(workflow):
    steps = workflow["jobs"]["digest"]["steps"]
    run_step = next(s for s in steps if s.get("id") == "pipeline")
    assert run_step["run"] == "python -m shorts_digest run"
    assert run_step["env"] == {k: f"${{{{ secrets.{k} }}}}" for k in ENV_KEYS}


def test_workflow_saves_state_even_when_the_run_fails(workflow):
    steps = workflow["jobs"]["digest"]["steps"]
    for name in ("Save database to the data branch", "Upload digest", "Upload database backup"):
        assert next(s for s in steps if s.get("name") == name)["if"] == "always()"
    save = next(s for s in steps if s.get("name") == "Save database to the data branch")["run"]
    assert "git push -q -f" in save and save.rstrip().endswith(" data")


def test_no_secret_values_in_repo_files():
    """Cheap guard: nothing that looks like a Google API key in tracked config/workflow."""
    for path in [WORKFLOW, *(ROOT / "config").glob("*"), ROOT / ".env.example"]:
        assert not re.search(r"AIza[0-9A-Za-z_-]{20,}", path.read_text()), path
