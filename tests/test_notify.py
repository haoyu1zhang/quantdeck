import json
from datetime import datetime, timedelta, timezone

import pytest

from quantdeck.live.watchdog import check_runs
from quantdeck.notify import Channel, Notifier, Webhook, channels_from_env
from quantdeck.risk import RiskManager
from quantdeck.config import RiskConfig


class Recorder(Channel):
    name = "rec"

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, title, text):
        if self.fail:
            raise OSError("down")
        self.sent.append((title, text))


def test_notifier_sends_in_background_and_dedupes():
    rec = Recorder()
    n = Notifier([rec], prefix="[x] ", cooldown=60)
    assert n.notify("error", "boom")
    assert not n.notify("error", "boom")  # within cooldown
    assert n.notify("fill", "buy BTC")
    assert n.notify("fill", "buy BTC")    # fills are never deduped
    assert not n.notify("unknown", "nope")
    n.flush(5)
    assert [t for t, _ in rec.sent] == ["[x] boom", "[x] buy BTC", "[x] buy BTC"]


def test_failing_channel_does_not_raise():
    bad, good = Recorder(fail=True), Recorder()
    n = Notifier([bad, good], cooldown=0)
    n.notify("halt", "halted")
    n.flush(5)
    assert good.sent


def test_webhook_formats():
    assert Webhook("u", "wecom").payload("t", "x") == {"msgtype": "text", "text": {"content": "t\nx"}}
    assert Webhook("u", "feishu").payload("t", "x")["msg_type"] == "text"
    assert "content" in Webhook("u", "discord").payload("t", "x")


def test_channels_from_env(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NOTIFY_WEBHOOK_URL", "SMTP_HOST", "NOTIFY_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert channels_from_env() == []
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.invalid/hook")
    assert [c.name for c in channels_from_env(webhook="slack")] == ["webhook"]
    assert channels_from_env(webhook=False) == []
    with pytest.raises(RuntimeError):
        channels_from_env(telegram=True)


def test_watchdog_flags_stale_and_recovery(tmp_path):
    now = datetime.now(timezone.utc)
    d = tmp_path / "bot"
    d.mkdir()

    def write(beat, state="running"):
        (d / "status.json").write_text(json.dumps({"name": "bot", "state": state, "heartbeat": beat.isoformat()}))

    alerted = set()
    write(now - timedelta(minutes=30))
    assert [e for e, *_ in check_runs(tmp_path, 600, alerted, now)] == ["stale"]
    assert check_runs(tmp_path, 600, alerted, now) == []  # only once
    write(now)
    assert [e for e, *_ in check_runs(tmp_path, 600, alerted, now)] == ["recovered"]
    write(now - timedelta(hours=5), state="stopped")
    assert check_runs(tmp_path, 600, alerted, now) == []


def test_daily_loss_halt_lifts_next_day():
    from datetime import date

    rm = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
    rm.new_day(date(2024, 1, 1), 100)
    rm.update_equity(90)
    assert rm.halted
    rm.new_day(date(2024, 1, 2), 90)
    assert not rm.halted
    rm.halt("too many consecutive errors")
    rm.new_day(date(2024, 1, 3), 90)
    assert rm.halted
