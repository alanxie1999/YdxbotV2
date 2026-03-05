import json
from pathlib import Path

import zq_multiuser as zm
from user_manager import UserContext


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_user(tmp_path: Path, user_id: int = 63001) -> UserContext:
    user_dir = tmp_path / "users" / str(user_id)
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态展示用户"},
            "telegram": {"user_id": user_id},
            "groups": {"admin_chat": user_id},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    return UserContext(str(user_dir))


def test_format_dashboard_includes_bet_status_line(tmp_path, monkeypatch):
    ctx = _make_user(tmp_path, user_id=63011)
    ctx.state.history = [1, 0] * 30
    ctx.state.runtime["bet_on"] = True
    monkeypatch.setattr(
        zm,
        "get_current_repo_info",
        lambda: {"current_tag": "v1.0.10", "nearest_tag": "v1.0.10", "short_commit": "abcd1234"},
    )

    msg = zm.format_dashboard(ctx)
    assert "🚦 **当前押注状态：运行中**" in msg


def test_get_bet_status_text_shows_pause_reason_and_remaining_rounds():
    rt = {
        "manual_pause": False,
        "switch": True,
        "bet_on": False,
        "stop_count": 3,
        "pause_countdown_active": True,
        "pause_countdown_total_rounds": 2,
        "pause_countdown_last_remaining": 2,
        "pause_countdown_reason": "深度风控暂停（3连输档）",
    }

    status = zm.get_bet_status_text(rt)
    assert status == "自动暂停（剩2局，深度风控暂停（3连输档））"

