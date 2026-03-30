"""Group market broadcast and alert tool.

This module is intentionally standalone:
- It manages its own config/state files
- It can reuse pure helpers from ``zq_multiuser``
- The main runtime does not need to import it
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

import zq_multiuser as zm


MODULE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = MODULE_DIR / "market_broadcast_alert_config.json"
STATE_PATH = MODULE_DIR / "market_broadcast_alert_state.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enable": False,
    "bot_token": "",
    "chat_id": 0,
    "streak_threshold": 4,
    "pair_trigger_consecutive": 3,
    "report_interval": 10,
    "cooldown_seconds": 600,
    "mention_users": [],
}

DEFAULT_STATE: Dict[str, Any] = {
    "last_update_id": 0,
    "last_history_fingerprint": "",
    "round_counter": 0,
    "market_history": [],
    "last_streak_side": "",
    "last_streak_notified_len": 0,
    "last_pair_tag": "",
    "last_pair_count": 0,
    "last_pair_notified_count": 0,
    "last_report_round": 0,
    "last_alert_at": {},
}


@dataclass
class AlertEvent:
    event_type: str
    message: str


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    merged = dict(default)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> Dict[str, Any]:
    config = _read_json(CONFIG_PATH, DEFAULT_CONFIG)
    config["streak_threshold"] = max(1, int(config.get("streak_threshold", 4) or 4))
    config["pair_trigger_consecutive"] = max(1, int(config.get("pair_trigger_consecutive", 3) or 3))
    config["report_interval"] = max(1, int(config.get("report_interval", 10) or 10))
    config["cooldown_seconds"] = max(0, int(config.get("cooldown_seconds", 600) or 600))
    mention_users = config.get("mention_users", [])
    if not isinstance(mention_users, list):
        mention_users = []
    config["mention_users"] = [str(item).strip() for item in mention_users if str(item).strip()]
    return config


def save_config(config: Dict[str, Any]) -> None:
    _write_json(CONFIG_PATH, config)


def load_state() -> Dict[str, Any]:
    state = _read_json(STATE_PATH, DEFAULT_STATE)
    if not isinstance(state.get("market_history"), list):
        state["market_history"] = []
    if not isinstance(state.get("last_alert_at"), dict):
        state["last_alert_at"] = {}
    return state


def save_state(state: Dict[str, Any]) -> None:
    _write_json(STATE_PATH, state)


def parse_market_history(text: str) -> List[int]:
    raw = str(text or "")
    history_str = raw.split("]")[-1] if "]" in raw else raw
    values = [int(x) for x in zm.re.findall(r"(?<!\d)[01](?!\d)", history_str)]
    return values


def get_current_streak(history: List[int]) -> tuple[int, int]:
    if not history:
        return 0, -1
    tail = int(history[-1])
    streak = 1
    for value in reversed(history[:-1]):
        if int(value) == tail:
            streak += 1
        else:
            break
    return streak, tail


def _cooldown_ready(state: Dict[str, Any], config: Dict[str, Any], key: str, now_ts: Optional[int] = None) -> bool:
    now = int(now_ts or time.time())
    last_map = state.get("last_alert_at", {})
    last_ts = int(last_map.get(key, 0) or 0)
    return now - last_ts >= int(config.get("cooldown_seconds", 600) or 600)


def _mark_alert_sent(state: Dict[str, Any], key: str, now_ts: Optional[int] = None) -> None:
    if "last_alert_at" not in state or not isinstance(state["last_alert_at"], dict):
        state["last_alert_at"] = {}
    state["last_alert_at"][key] = int(now_ts or time.time())


def _format_history_block(history: List[int], width: int = 10, rows: int = 4) -> str:
    recent = history[-(width * rows):]
    if not recent:
        return "[ ]"
    lines: List[str] = []
    for start in range(0, len(recent), width):
        row = recent[start:start + width]
        lines.append("[ " + " ".join(str(x) for x in row) + " ]")
    return "\n".join(lines)


def _format_mentions(config: Dict[str, Any]) -> str:
    mention_users = config.get("mention_users", [])
    if not mention_users:
        return ""
    return "\n\n" + " ".join(mention_users)


def build_streak_alert(history: List[int], config: Dict[str, Any]) -> Optional[str]:
    streak_len, side = get_current_streak(history)
    threshold = int(config.get("streak_threshold", 4) or 4)
    if streak_len < threshold or side not in {0, 1}:
        return None

    alert_type = "连大提醒" if side == 1 else "连小提醒"
    advice_side = "小" if side == 1 else "大"
    rule_text = f"当前盘口已出现 {streak_len} 连{'大' if side == 1 else '小'}"
    return (
        "🚨 群重点提醒 🚨\n\n"
        f"类型：{alert_type}\n"
        f"规律：{rule_text}\n"
        "说明：盘口单边偏移明显，可能接近反切位\n"
        "人工建议：可观察手动反投\n"
        f"建议手动下注：{advice_side}\n\n"
        "近40局（由近及远）\n"
        f"{_format_history_block(history[-40:])}"
        f"{_format_mentions(config)}"
    )


def build_pair_alert(history: List[int], config: Dict[str, Any]) -> Optional[str]:
    rhythm = zm.analyze_rhythm_context(history)
    threshold = int(config.get("pair_trigger_consecutive", 3) or 3)
    tag = str(rhythm.get("rhythm_tag", ""))
    if tag not in {"ALTERNATION_RHYTHM", "PAIR_FORMATION"}:
        return None

    if tag == "ALTERNATION_RHYTHM":
        next_char = rhythm.get("alternation_next")
        if next_char not in {0, 1}:
            return None
        advice_side = "大" if int(next_char) == 0 else "小"
        return (
            "🚨 群重点提醒 🚨\n\n"
            "类型：配对规律提醒\n"
            f"规律：当前盘口连续识别为交替型（{rhythm.get('recent_seq', '')}）\n"
            "说明：盘口处于明显交替节奏，当前可观察其结束交替的反切机会\n"
            "人工建议：可观察手动反投，尝试结束交替规律\n"
            f"建议手动下注：{advice_side}\n\n"
            "近40局（由近及远）\n"
            f"{_format_history_block(history[-40:])}"
            f"{_format_mentions(config)}"
        )

    return (
        "🚨 群重点提醒 🚨\n\n"
        "类型：配对规律提醒\n"
        f"规律：当前盘口连续识别为成双型（{rhythm.get('recent_seq', '')}）\n"
        "说明：当前属于配对节奏，请人工观察，不直接给下注建议\n"
        "人工建议：等待更明确的下一步节奏确认\n\n"
        "近40局（由近及远）\n"
        f"{_format_history_block(history[-40:])}"
        f"{_format_mentions(config)}"
    )


def build_market_stats_report(history: List[int], report_interval: int, config: Dict[str, Any]) -> str:
    windows = [1000, 500, 200, 100]
    labels: List[int] = []
    stats = {"连大": [], "连小": []}
    all_ns = set()

    result_counts_full = zm.count_consecutive(history)
    for window in windows:
        actual = min(int(window), len(history))
        if actual <= 0:
            continue
        if actual in labels:
            continue
        labels.append(actual)
        result_counts = zm.count_consecutive(history[-actual:])
        stats["连大"].append(result_counts["大"])
        stats["连小"].append(result_counts["小"])
        all_ns.update(result_counts["大"].keys())
        all_ns.update(result_counts["小"].keys())

    label_width = max(3, max(len(str(label)) for label in labels)) if labels else 3
    header = "类别 |" + "".join(f" {str(label).rjust(label_width)} |" for label in labels)
    divider = "-" * len(header)

    lines = [f"说明：最近新增 {report_interval} 局盘口，推送一次连大连小统计", ""]
    for category in ["连大", "连小"]:
        lines.append(category)
        lines.append("=" * len(header))
        lines.append(header)
        lines.append(divider)
        for n in sorted(all_ns, reverse=True):
            if any(n in stats[category][i] for i in range(len(labels))):
                row = f" {str(n).center(2)}  |"
                for i in range(len(labels)):
                    count = stats[category][i].get(n, 0)
                    value = str(count) if count > 0 else "-"
                    row += f" {value.center(label_width)} |"
                lines.append(row)
        lines.append("")

    return (
        "📊 群盘口统计播报 📊\n\n"
        + "\n".join(lines).rstrip()
        + "\n\n近40局（由近及远）\n"
        + _format_history_block(history[-40:])
        + _format_mentions(config)
    )


def update_market_state(state: Dict[str, Any], history: List[int]) -> bool:
    fingerprint = "".join(str(x) for x in history[-40:])
    if not history or state.get("last_history_fingerprint", "") == fingerprint:
        return False
    state["last_history_fingerprint"] = fingerprint
    state["market_history"] = history[-2000:]
    state["round_counter"] = int(state.get("round_counter", 0) or 0) + 1
    return True


def evaluate_alerts(state: Dict[str, Any], config: Dict[str, Any], history: List[int]) -> List[AlertEvent]:
    events: List[AlertEvent] = []
    if not history or not bool(config.get("enable", False)):
        return events

    streak_len, side = get_current_streak(history)
    streak_key = f"streak_{side}_{streak_len}"
    threshold = int(config.get("streak_threshold", 4) or 4)
    if streak_len >= threshold and _cooldown_ready(state, config, streak_key):
        message = build_streak_alert(history, config)
        if message:
            events.append(AlertEvent("streak", message))
            _mark_alert_sent(state, streak_key)

    rhythm = zm.analyze_rhythm_context(history)
    rhythm_tag = str(rhythm.get("rhythm_tag", ""))
    prev_tag = str(state.get("last_pair_tag", "") or "")
    prev_count = int(state.get("last_pair_count", 0) or 0)
    if rhythm_tag in {"ALTERNATION_RHYTHM", "PAIR_FORMATION"}:
        current_count = prev_count + 1 if rhythm_tag == prev_tag else 1
        state["last_pair_tag"] = rhythm_tag
        state["last_pair_count"] = current_count
        notify_threshold = int(config.get("pair_trigger_consecutive", 3) or 3)
        pair_key = f"pair_{rhythm_tag}_{current_count}"
        if current_count >= notify_threshold and _cooldown_ready(state, config, pair_key):
            message = build_pair_alert(history, config)
            if message:
                events.append(AlertEvent("pair", message))
                _mark_alert_sent(state, pair_key)
    else:
        state["last_pair_tag"] = ""
        state["last_pair_count"] = 0

    report_interval = int(config.get("report_interval", 10) or 10)
    round_counter = int(state.get("round_counter", 0) or 0)
    last_report_round = int(state.get("last_report_round", 0) or 0)
    if round_counter >= report_interval and round_counter - last_report_round >= report_interval:
        events.append(AlertEvent("report", build_market_stats_report(history, report_interval, config)))
        state["last_report_round"] = round_counter

    return events


def _is_admin(bot_token: str, chat_id: int, user_id: int) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/getChatMember"
    response = requests.get(url, params={"chat_id": chat_id, "user_id": user_id}, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        return False
    status = str(data.get("result", {}).get("status", "")).lower()
    return status in {"administrator", "creator"}


def _send_text(bot_token: str, chat_id: int, text: str) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    response.raise_for_status()
    return response.json()


def _normalize_command(text: str) -> List[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if raw.startswith("/"):
        raw = raw[1:]
    raw = raw.replace("@", " @")
    return [part.strip() for part in raw.split() if part.strip()]


def handle_command(text: str, sender_id: int, config: Dict[str, Any]) -> Optional[str]:
    tokens = _normalize_command(text)
    if not tokens:
        return None
    if tokens[0].lower() != "fa":
        return None

    bot_token = str(config.get("bot_token", "") or "").strip()
    chat_id = int(config.get("chat_id", 0) or 0)
    if bot_token and chat_id and sender_id:
        try:
            if not _is_admin(bot_token, chat_id, sender_id):
                return "❌ 仅群管理员可使用该命令"
        except Exception:
            return "❌ 管理员身份校验失败，请稍后再试"

    if len(tokens) == 1:
        mentions = " ".join(config.get("mention_users", [])) or "未设置"
        status = "ON" if bool(config.get("enable", False)) else "OFF"
        return (
            "📡 盘口播报提醒配置\n\n"
            f"开关：{status}\n"
            f"连大连小阈值：{config.get('streak_threshold', 4)}\n"
            f"配对规律阈值：{config.get('pair_trigger_consecutive', 3)}\n"
            f"周期播报间隔：{config.get('report_interval', 10)}\n"
            f"艾特名单：{mentions}"
        )

    sub = tokens[1].lower()
    if sub in {"on", "off"}:
        config["enable"] = sub == "on"
        save_config(config)
        return f"✅ 已{'开启' if config['enable'] else '关闭'}盘口播报提醒"

    if sub == "s" and len(tokens) >= 3:
        config["streak_threshold"] = max(1, int(tokens[2]))
        save_config(config)
        return f"✅ 连大连小提醒阈值已设置为 {config['streak_threshold']}"

    if sub == "p" and len(tokens) >= 3:
        config["pair_trigger_consecutive"] = max(1, int(tokens[2]))
        save_config(config)
        return f"✅ 配对规律提醒阈值已设置为 {config['pair_trigger_consecutive']}"

    if sub == "r" and len(tokens) >= 3:
        config["report_interval"] = max(1, int(tokens[2]))
        save_config(config)
        return f"✅ 盘口统计播报间隔已设置为 {config['report_interval']}"

    if sub == "m":
        mention_users = list(config.get("mention_users", []))
        if len(tokens) == 2:
            return "📡 当前艾特名单\n\n" + ("\n".join(mention_users) if mention_users else "未设置")
        action = tokens[2]
        payload = [item for item in tokens[3:] if item.startswith("@")]
        if action == "+":
            merged = mention_users + [item for item in payload if item not in mention_users]
            config["mention_users"] = merged
            save_config(config)
            return "✅ 已添加艾特名单\n\n" + ("\n".join(payload) if payload else "未添加任何用户")
        if action == "-":
            config["mention_users"] = [item for item in mention_users if item not in payload]
            save_config(config)
            return "✅ 已删除艾特名单\n\n" + ("\n".join(payload) if payload else "未删除任何用户")

    return (
        "📡 fa 命令说明\n\n"
        "fa\n"
        "fa on / fa off\n"
        "fa s 4\n"
        "fa p 3\n"
        "fa r 10\n"
        "fa m\n"
        "fa m + @user1 @user2\n"
        "fa m - @user1"
    )


def process_group_message(message: Dict[str, Any], config: Dict[str, Any], state: Dict[str, Any]) -> List[AlertEvent]:
    text = str(message.get("text", "") or "")
    history = parse_market_history(text)
    if not history:
        return []
    updated = update_market_state(state, history)
    if not updated:
        return []
    return evaluate_alerts(state, config, history)


def run_forever(sleep_seconds: int = 3) -> None:
    config = load_config()
    state = load_state()
    bot_token = str(config.get("bot_token", "") or "").strip()
    chat_id = int(config.get("chat_id", 0) or 0)
    if not bot_token or not chat_id:
        raise RuntimeError("请先在 market_broadcast_alert_config.json 中配置 bot_token 和 chat_id")

    while True:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        response = requests.get(
            url,
            params={
                "offset": int(state.get("last_update_id", 0) or 0),
                "timeout": 20,
                "allowed_updates": ["message"],
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        updates = payload.get("result", []) if isinstance(payload, dict) else []
        for update in updates:
            update_id = int(update.get("update_id", 0) or 0)
            state["last_update_id"] = update_id + 1
            message = update.get("message", {}) if isinstance(update.get("message", {}), dict) else {}
            if int(message.get("chat", {}).get("id", 0) or 0) != chat_id:
                continue
            text = str(message.get("text", "") or "")
            sender_id = int(message.get("from", {}).get("id", 0) or 0)
            command_reply = handle_command(text, sender_id, config)
            if command_reply:
                _send_text(bot_token, chat_id, command_reply)
                continue
            for event in process_group_message(message, config, state):
                _send_text(bot_token, chat_id, event.message)
        save_state(state)
        time.sleep(max(1, int(sleep_seconds)))


if __name__ == "__main__":
    run_forever()
