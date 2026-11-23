"""Trigger nodes and the cron parser."""

from __future__ import annotations

import importlib
from datetime import datetime

import pytest
from agenticmind_node_sdk.testing import invoke_node

tr = importlib.import_module("agenticmind_nodes_core.triggers")
cron = importlib.import_module("agenticmind_nodes_core.cron")


def nxt(expr: str, after: str, tz: str = "UTC") -> str:
    when = cron.CronExpression.parse(expr).next_after(datetime.fromisoformat(after), tz)
    return when.isoformat()


def test_cron_fields_and_next_times():
    assert nxt("*/15 * * * *", "2026-11-23T10:07:30+00:00") == "2026-11-23T10:15:00+00:00"
    assert nxt("0 9 * * 1-5", "2026-11-21T12:00:00+00:00") == "2026-11-23T09:00:00+00:00"  # Sat -> Mon
    assert nxt("30 8 1 jan *", "2026-11-23T00:00:00+00:00") == "2027-01-01T08:30:00+00:00"
    assert nxt("0 0 29 2 *", "2026-03-01T00:00:00+00:00") == "2028-02-29T00:00:00+00:00"  # leap day
    assert nxt("@hourly", "2026-11-23T10:00:00+00:00") == "2026-11-23T11:00:00+00:00"
    assert nxt("0 12 * * sun", "2026-11-23T00:00:00+00:00") == nxt("0 12 * * 7", "2026-11-23T00:00:00+00:00")
    # both day fields restricted: either may match (standard cron)
    assert nxt("0 0 1 * mon", "2026-11-23T01:00:00+00:00") == "2026-11-30T00:00:00+00:00"
    # 09:00 in New York is 14:00 UTC in winter
    assert nxt("0 9 * * *", "2026-11-23T00:00:00+00:00", "America/New_York") == "2026-11-23T14:00:00+00:00"


@pytest.mark.parametrize("bad", ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"])
def test_cron_rejects_bad_expressions(bad):
    with pytest.raises(ValueError):
        cron.CronExpression.parse(bad)


def test_schedule_config_validation():
    with pytest.raises(Exception, match="5 fields"):
        tr.ScheduleTriggerConfig.model_validate({"cron": "* *"})
    with pytest.raises(Exception, match="time zone"):
        tr.ScheduleTriggerConfig.model_validate({"timezone": "Mars/Olympus"})


async def test_trigger_nodes_use_the_event_or_their_sample():
    sample = await invoke_node(tr.webhook_trigger, config={"sample": {"a": 1}})
    assert sample.outputs["payload"] == {"a": 1} and sample.outputs["event"]["simulated"] is True
    event = {"node": "node_under_test", "kind": "webhook", "payload": {"order": 7}, "text": "", "sender": None}
    real = await invoke_node(tr.webhook_trigger, run_input={"_trigger": event})
    assert real.outputs["payload"] == {"order": 7} and real.outputs["event"]["simulated"] is False
    assert real.outputs["text"] == '{"order": 7}'
    other = await invoke_node(tr.webhook_trigger, run_input={"_trigger": {**event, "node": "another"}})
    assert other.outputs["event"]["simulated"] is True  # an event for a different trigger node
    chat = await invoke_node(tr.chat_trigger, config={"sample_text": "hello bot"})
    assert chat.outputs["text"] == "hello bot" and chat.outputs["payload"]["channel"] == "telegram"
