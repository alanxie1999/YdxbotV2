from pathlib import Path
from types import SimpleNamespace

import market_broadcast_alert.market_broadcast_alert as mba
import zq_multiuser as zm


def test_parse_market_history_extracts_binary_sequence():
    text = "[近 40 次结果][由近及远][0 小 1 大] 1 0 1 1 0 0 1"

    result = mba.parse_market_history(text)

    assert result == [1, 0, 1, 1, 0, 0, 1]


def test_build_streak_alert_recommends_reverse_side():
    history = [0, 1, 1, 1, 1]
    config = {"streak_threshold": 4, "mention_users": ["@a", "@b"]}

    message = mba.build_streak_alert(history, config)

    assert "<b>🚨 群重点提醒 🚨</b>" in message
    assert "连大提醒" in message
    assert "建议手动下注：小" in message
    assert "✅" in message and "❌" in message
    assert message.rstrip().endswith("@a @b")


def test_build_pair_alert_for_alternation_gives_reverse_suggestion():
    history = [0, 1, 0, 1, 0, 1, 0, 1]
    config = {"pair_trigger_consecutive": 3, "mention_users": ["@a"]}

    message = mba.build_pair_alert(history, config)

    assert "配对规律提醒" in message
    assert "交替型" in message
    assert "建议手动下注：" in message
    assert message.rstrip().endswith("@a")


def test_build_pair_alert_for_pair_formation_has_no_bet_side():
    history = [1, 0, 1, 1, 0, 1, 1, 0, 1]
    config = {"pair_trigger_consecutive": 3, "mention_users": ["@a"]}

    message = mba.build_pair_alert(history, config)

    assert "配对规律提醒" in message
    assert "成双型" in message
    assert "建议手动下注：" not in message
    assert message.rstrip().endswith("@a")


def test_handle_command_updates_threshold_and_mentions(tmp_path, monkeypatch):
    config_path = tmp_path / "cfg.json"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(mba, "CONFIG_PATH", config_path)
    monkeypatch.setattr(mba, "STATE_PATH", state_path)

    config = mba.load_config()

    result = mba.handle_command("fa s 6", sender_id=0, config=config)
    assert "6" in result
    config = mba.load_config()
    assert config["streak_threshold"] == 6

    result = mba.handle_command("fa m + @u1 @u2", sender_id=0, config=config)
    assert "已添加艾特名单" in result
    config = mba.load_config()
    assert config["mention_users"] == ["@u1", "@u2"]

    result = mba.handle_command("fa r off", sender_id=0, config=config)
    assert "关闭周期统计播报" in result
    config = mba.load_config()
    assert config["report_enable"] is False

    result = mba.handle_command("fa r on", sender_id=0, config=config)
    assert "开启周期统计播报" in result
    config = mba.load_config()
    assert config["report_enable"] is True


def test_process_group_message_updates_state_and_triggers_report():
    config = {
        "enable": True,
        "streak_threshold": 20,
        "pair_trigger_consecutive": 99,
        "report_interval": 1,
        "cooldown_seconds": 0,
        "mention_users": [],
    }
    state = dict(mba.DEFAULT_STATE)
    message = {"text": "[近 40 次结果][由近及远][0 小 1 大] 1 0 1 0 1 0 1 0 1 0"}

    events = mba.process_group_message(message, config, state)

    assert state["round_counter"] == 1
    assert any(event.event_type == "report" for event in events)


def test_validate_runtime_config_requires_full_bot_token():
    config = {
        "bot_token": "AAGZD7pquDgGxvn_QnjTIP5s7QQqUHB6K0A",
        "chat_id": -1001234567890,
    }

    try:
        mba.validate_runtime_config(config)
    except ValueError as exc:
        assert "完整 token" in str(exc)
    else:
        raise AssertionError("expected invalid token to be rejected")


def test_process_market_history_snapshot_uses_main_history_not_group_message(tmp_path, monkeypatch):
    config_path = tmp_path / "cfg.json"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(mba, "CONFIG_PATH", config_path)
    monkeypatch.setattr(mba, "STATE_PATH", state_path)

    sent_messages = []

    deleted_messages = []
    scheduled = []

    def _fake_send_text(bot_token, chat_id, text, parse_mode=None):
        sent_messages.append((bot_token, chat_id, text))
        return {"ok": True, "result": {"message_id": 100 + len(sent_messages)}}

    def _fake_delete(bot_token, chat_id, message_id):
        deleted_messages.append((chat_id, message_id))

    def _fake_schedule(bot_token, chat_id, message_id, delay_seconds):
        scheduled.append((chat_id, message_id, delay_seconds))

    monkeypatch.setattr(mba, "_send_text", _fake_send_text)
    monkeypatch.setattr(mba, "_delete_message", _fake_delete)
    monkeypatch.setattr(mba, "_schedule_delete", _fake_schedule)

    mba.save_config(
        {
            "enable": True,
            "bot_token": "8743311990:AAGZD7pquDgGxvn_QnjTIP5s7QQqUHB6K0A",
            "chat_id": -1002310838908,
            "allowed_sender_ids": [5721909476],
            "report_enable": True,
            "streak_threshold": 4,
            "pair_trigger_consecutive": 3,
            "report_interval": 10,
            "cooldown_seconds": 0,
            "mention_users": ["@TrumpChe"],
        }
    )

    sent = mba.process_market_history_snapshot([1, 0, 1, 1, 1, 1])

    assert sent == 1
    assert sent_messages
    assert sent_messages[0][1] == -1002310838908
    assert "连大提醒" in sent_messages[0][2]
    assert scheduled and scheduled[0][2] == mba.AUTO_DELETE_SECONDS
    assert not deleted_messages

    sent_again = mba.process_market_history_snapshot([1, 0, 1, 1, 1, 1])
    assert sent_again == 0


def test_build_market_stats_report_has_no_mentions_or_recent_history():
    history = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1] * 20
    config = {"mention_users": ["@a", "@b"]}

    message = mba.build_market_stats_report(history, 10, config)

    assert "<b>📊 群盘口统计播报 📊</b>" in message
    assert "<pre>" in message
    assert "近40局" not in message
    assert "@a" not in message and "@b" not in message


def test_build_stats_report_separates_market_and_bet_statistics():
    state = SimpleNamespace(
        history=[1, 1, 1, 0, 0, 1, 1, 0],
        bet_sequence_log=[
            {"result": "赢", "profit": 100},
            {"result": "输", "profit": -100},
            {"result": "输", "profit": -100},
            {"result": "赢", "profit": 100},
        ],
    )

    report = zm._build_stats_report(state, windows=[8, 4])

    assert "盘口统计" in report
    assert "押注统计" in report
    assert "连大" in report
    assert "连小" in report
    assert "连输" in report
