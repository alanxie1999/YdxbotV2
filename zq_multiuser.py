"""
zq_multiuser.py - 多用户版本核心逻辑
版本: 2.4.3
日期: 2026-02-21
功能: 多用户押注、结算、命令处理
"""

import logging
import asyncio
import json
import os
import re
import requests
import aiohttp
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
from user_manager import UserContext, UserState, trim_bet_sequence_log
from typing import Dict, Any, List, Optional
import constants
from update_manager import (
    get_current_repo_info,
    list_version_catalog,
    reback_to_version,
    resolve_systemd_service_name,
    restart_process,
    update_to_version,
)

# 日志配置
logger = logging.getLogger('zq_multiuser')
logger.setLevel(logging.DEBUG)
logger.propagate = False

ACCOUNT_LOG_ROOT = os.path.join("logs", "accounts")
_ACCOUNT_NAME_REGISTRY: Dict[str, str] = {}


def _sanitize_account_slug(text: str, fallback: str = "unknown") -> str:
    raw = str(text or "").strip().lower().replace(" ", "-")
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
    return cleaned or fallback


def _build_account_label(account_slug: str) -> str:
    return f"ydx-{account_slug}"


def _resolve_account_identity(
    user_ctx: Optional[UserContext] = None,
    user_id: Any = 0,
    account_name: str = "",
) -> Dict[str, str]:
    user_id_text = str(user_id or 0)
    resolved_name = str(account_name or "").strip()
    if user_ctx is not None:
        user_id_text = str(getattr(user_ctx, "user_id", user_id_text) or user_id_text)
        if not resolved_name:
            resolved_name = str(getattr(getattr(user_ctx, "config", None), "name", "") or "").strip()
    if not resolved_name and user_id_text not in {"", "0"}:
        resolved_name = f"user-{user_id_text}"
    account_slug = _sanitize_account_slug(
        resolved_name,
        fallback=(f"user-{user_id_text}" if user_id_text not in {"", "0"} else "unknown"),
    )
    return {
        "user_id": user_id_text,
        "account_name": resolved_name,
        "account_slug": account_slug,
        "account_label": _build_account_label(account_slug),
        "account_tag": f"【ydx-{account_slug}】",
    }


def register_user_log_identity(user_ctx: UserContext) -> str:
    """注册账号日志标识，供统一日志前缀和分流使用。"""
    user_id = str(getattr(user_ctx, "user_id", 0) or 0)
    account_name = str(getattr(getattr(user_ctx, "config", None), "name", "") or "").strip()
    if not account_name:
        account_name = f"user-{user_id}"
    _ACCOUNT_NAME_REGISTRY[user_id] = account_name
    return account_name


def _infer_log_category(level: int, module: str, event: str) -> str:
    if level >= logging.WARNING:
        return "warning"
    text = f"{module}:{event}".lower()
    business_tokens = (
        "bet", "settle", "risk", "predict", "user_cmd", "balance", "fund",
        "profit", "preset", "pause", "resume", "restart", "update", "reback",
        "model", "apikey", "stats", "status", "yc", "dashboard",
    )
    if any(token in text for token in business_tokens):
        return "business"
    return "runtime"


class _LogDefaultsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "user_id"):
            record.user_id = "0"
        if not hasattr(record, "mod"):
            record.mod = "zq"
        if not hasattr(record, "event"):
            record.event = "general"
        if not hasattr(record, "data"):
            record.data = ""
        if not hasattr(record, "category"):
            record.category = _infer_log_category(record.levelno, str(record.mod), str(record.event))
        if not hasattr(record, "account_slug"):
            fallback_slug = f"user-{record.user_id}" if str(record.user_id) != "0" else "unknown"
            record.account_slug = _sanitize_account_slug("", fallback=fallback_slug)
        if not hasattr(record, "account_tag"):
            record.account_tag = f"【ydx-{record.account_slug}】"
        return True


class _AccountCategoryRouterHandler(logging.Handler):
    """按账号+分类分流到独立日志文件：logs/accounts/<账号>/<runtime|warning|business>.log"""

    def __init__(self, root_dir: str, backup_count: int = 7):
        super().__init__(level=logging.DEBUG)
        self.root_dir = root_dir
        self.backup_count = backup_count
        self._handlers: Dict[tuple, TimedRotatingFileHandler] = {}
        self._default_filter = _LogDefaultsFilter()
        self._formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] [%(mod)s:%(event)s] %(message)s | %(data)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    def _get_handler(self, account_slug: str, category: str) -> TimedRotatingFileHandler:
        key = (account_slug, category)
        if key in self._handlers:
            return self._handlers[key]

        account_dir = os.path.join(self.root_dir, account_slug)
        os.makedirs(account_dir, exist_ok=True)
        log_path = os.path.join(account_dir, f"{category}.log")
        handler = TimedRotatingFileHandler(
            log_path,
            when='midnight',
            interval=1,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        handler.setFormatter(self._formatter)
        handler.addFilter(self._default_filter)
        self._handlers[key] = handler
        return handler

    def emit(self, record: logging.LogRecord):
        try:
            self._default_filter.filter(record)
            account_slug = str(getattr(record, "account_slug", "unknown") or "unknown")
            category = str(getattr(record, "category", "runtime") or "runtime")
            handler = self._get_handler(account_slug, category)
            handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self):
        for handler in self._handlers.values():
            try:
                handler.close()
            except Exception:
                pass
        self._handlers.clear()
        super().close()


_default_log_filter = _LogDefaultsFilter()

file_handler = TimedRotatingFileHandler('bot.log', when='midnight', interval=1, backupCount=7, encoding='utf-8')
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] [%(mod)s:%(event)s] %(message)s | %(data)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
file_handler.addFilter(_default_log_filter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] %(message)s',
    datefmt='%H:%M:%S'
))
console_handler.addFilter(_default_log_filter)
logger.addHandler(console_handler)

account_category_handler = _AccountCategoryRouterHandler(ACCOUNT_LOG_ROOT, backup_count=7)
account_category_handler.addFilter(_default_log_filter)
logger.addHandler(account_category_handler)


class _AccountIdentityFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        account_slug = str(getattr(record, "account_slug", "") or "").strip()
        if not account_slug:
            fallback_slug = f"user-{getattr(record, 'user_id', '0')}" if str(getattr(record, "user_id", "0")) != "0" else "unknown"
            account_slug = _sanitize_account_slug("", fallback=fallback_slug)
            record.account_slug = account_slug
        record.account_label = _build_account_label(account_slug)
        record.account_tag = f"【ydx-{account_slug}】"
        return True


_account_identity_filter = _AccountIdentityFilter()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(account_label)s | %(message)s',
    datefmt='%H:%M:%S'
))
console_handler.setLevel(logging.INFO)
console_handler.addFilter(_account_identity_filter)
account_category_handler.addFilter(_account_identity_filter)
try:
    logger.removeHandler(file_handler)
    file_handler.close()
except Exception:
    pass

# 自动统计推送节奏：每 10 局一次，保留 10 分钟后自动删除
AUTO_STATS_INTERVAL_ROUNDS = 10
AUTO_STATS_DELETE_DELAY_SECONDS = 600

# 风控节奏：以最近 40 笔实盘胜率为基础，结合连输深度做分层暂停。
RISK_WINDOW_BETS = 40
RISK_BASE_TRIGGER_WINS = 15          # 15/40=37.5%
RISK_BASE_TRIGGER_STREAK_NEEDED = 2   # 连续2次命中基础风控才触发暂停
RISK_RECOVERY_WINS = 19              # >45% => 至少 19/40
RISK_RECOVERY_PASS_NEEDED = 2         # 连续2次满足恢复条件才重置风险周期

# 深度风控触发节奏（不占基础风控预算）：
# 每连输 3 局触发一次；首次触发上限更高，后续触发保持保守暂停。
RISK_DEEP_TRIGGER_INTERVAL = 3
RISK_DEEP_FIRST_MAX_PAUSE_ROUNDS = 5
RISK_DEEP_NEXT_MAX_PAUSE_ROUNDS = 3
# 长龙盘面下，深度风控做“小幅放宽”，避免长时间停摆。
RISK_DEEP_LONG_DRAGON_TAIL_LEN = 5
RISK_DEEP_LONG_DRAGON_MAX_PAUSE_ROUNDS = 2
RISK_BASE_MAX_PAUSE_ROUNDS = 10

# 基础风控预算：同一基础风控周期累计暂停不超过10局（深度风控不占用）
RISK_PAUSE_TOTAL_CAP_ROUNDS = 10
RISK_PAUSE_MODEL_TIMEOUT_SEC = 3.5
AI_KEY_WARNING_TEXT = "⚠️ 大模型AI key 失效/缺失，请更新 key！！！"

# 高倍入场质量门控（目标：尽量减少进入第5手以后）
ENTRY_GUARD_STEP3_MIN_CONF = 68
ENTRY_GUARD_STEP3_PAUSE_ROUNDS = 2
ENTRY_GUARD_STEP4_MIN_CONF = 70
ENTRY_GUARD_STEP4_MIN_CONF_EARLY = 68
ENTRY_GUARD_STEP4_PAUSE_ROUNDS = 3
ENTRY_GUARD_STEP4_ALLOWED_TAGS = {"DRAGON_CANDIDATE", "SINGLE_JUMP", "SYMMETRIC_WRAP"}
UNSTABLE_PATTERN_TAGS = {"CHAOS_SWITCH", "SINGLE_JUMP", "SYMMETRIC_WRAP"}
HIGH_PRESSURE_SKIP_MIN_STEP = 5
HIGH_PRESSURE_SKIP_MIN_CONF = 78
UNSTABLE_PATTERN_MIN_CONF_STEP3 = 72
UNSTABLE_PATTERN_MIN_CONF_STEP5 = 78
DRAGON_CANDIDATE_MIN_TAIL_STEP5 = 4
NEUTRAL_LONG_TERM_GAP_LOW = 0.47
NEUTRAL_LONG_TERM_GAP_HIGH = 0.53
HIGH_PRESSURE_PATTERN_PAUSE_ROUNDS = 2

# 高阶入场二次确认（第7手起，避免第5/6手过早双模型互卡）
HIGH_STEP_DOUBLE_CONFIRM_MIN_STEP = 7
HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF = 70
HIGH_STEP_DOUBLE_CONFIRM_PAUSE_ROUNDS = 2
HIGH_STEP_DOUBLE_CONFIRM_MODEL_TIMEOUT_SEC = 4.0

# 同手位防卡死：避免 SKIP/超时导致长期不落单
STALL_GUARD_SKIP_MAX = 2
STALL_GUARD_TIMEOUT_MAX = 2
STALL_GUARD_TOTAL_MAX = 6

# 暂停结束后的影子验证（只预测不下注）
SHADOW_PROBE_ENABLED = True
SHADOW_PROBE_ROUNDS = 3
SHADOW_PROBE_PASS_REQUIRED = 2
SHADOW_PROBE_RETRY_PAUSE_ROUNDS = 2


def log_event(level, module, event, message=None, **kwargs):
    # 兼容旧调用: log_event(level, event, message, user_id, data)
    if message is None:
        message = event
        event = module
        module = 'zq'
    category = str(kwargs.pop("category", "")).strip().lower()
    account_name = str(kwargs.pop("account_name", "")).strip()
    user_id = kwargs.get('user_id', 0)
    user_id_text = str(user_id)
    if not account_name:
        account_name = _ACCOUNT_NAME_REGISTRY.get(user_id_text, "")
    if not account_name and user_id_text not in {"", "0"}:
        account_name = f"user-{user_id_text}"
    account_slug = _sanitize_account_slug(account_name, fallback=(f"user-{user_id_text}" if user_id_text not in {"", "0"} else "unknown"))
    if category not in {"runtime", "warning", "business"}:
        category = _infer_log_category(level, str(module), str(event))
    data = ', '.join(f'{k}={v}' for k, v in kwargs.items())
    # 使用 'mod' 而不是 'module'，因为 'module' 是 logging 的保留字段
    logger.log(
        level,
        message,
        extra={
            'user_id': user_id_text,
            'mod': module,
            'event': event,
            'data': data,
            'category': category,
            'account_slug': account_slug,
            'account_tag': f"【ydx-{account_slug}】",
        },
    )


# 格式化数字
def format_number(num):
    """与 master 版一致：使用千分位格式。"""
    return f"{int(num):,}"


def _sync_fund_from_account_when_insufficient(rt: Dict[str, Any], required_amount: int = 0) -> bool:
    """
    仅在“资金不足”场景触发的修正：
    若当前菠菜资金不足，且账户余额更高，则把菠菜资金同步为账户余额。
    """
    try:
        fund = int(rt.get("gambling_fund", 0) or 0)
        balance = int(rt.get("account_balance", 0) or 0)
        need = max(0, int(required_amount or 0))
    except (TypeError, ValueError):
        return False

    threshold = max(1, need)
    if fund < threshold and balance > fund:
        rt["gambling_fund"] = balance
        return True
    return False


def heal_stale_pending_bets(user_ctx: UserContext) -> Dict[str, Any]:
    """
    启动时自愈历史挂单：
    - 仅允许“最后一笔且 runtime.bet=True”保持 result=None（真实待结算）
    - 其他 result=None 一律标记为“异常未结算”，避免历史统计与资金核对长期受污染
    """
    state = user_ctx.state
    rt = state.runtime
    logs = state.bet_sequence_log if isinstance(state.bet_sequence_log, list) else []
    if not logs:
        return {"count": 0, "items": []}

    pending_active = bool(rt.get("bet", False))
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    healed_items: List[str] = []

    for idx, item in enumerate(logs):
        if not isinstance(item, dict):
            continue
        if item.get("result") is not None:
            continue

        is_last = (idx == len(logs) - 1)
        if is_last and pending_active:
            # 正常待结算，不处理
            continue

        item["result"] = "异常未结算"
        if item.get("profit") is None:
            item["profit"] = 0
        item["heal_time"] = now_text
        item["heal_note"] = "startup_auto_heal_pending_bet"
        healed_items.append(str(item.get("bet_id") or f"index:{idx}"))

    healed_count = len(healed_items)
    if healed_count > 0:
        rt["pending_bet_heal_total"] = int(rt.get("pending_bet_heal_total", 0) or 0) + healed_count
        rt["pending_bet_last_heal_count"] = healed_count
        rt["pending_bet_last_heal_at"] = now_text

    return {"count": healed_count, "items": healed_items}


def _get_strategy_bet_sequence_log(state: UserState) -> List[Dict[str, Any]]:
    """Return the bet log slice that belongs to the current betting strategy chain."""
    logs = state.bet_sequence_log if isinstance(state.bet_sequence_log, list) else []
    rt = state.runtime if isinstance(getattr(state, "runtime", None), dict) else {}
    try:
        reset_index = int(rt.get("bet_reset_log_index", 0) or 0)
    except (TypeError, ValueError):
        reset_index = 0
    reset_index = max(0, min(reset_index, len(logs)))
    if reset_index <= 0:
        return logs
    if reset_index >= len(logs):
        return []
    return logs[reset_index:]


def _get_latest_open_bet_entry(state: UserState) -> Optional[Dict[str, Any]]:
    """返回最新一条未结算押注，供重复下注保护和结算对位使用。"""
    logs = _get_strategy_bet_sequence_log(state)
    for item in reversed(logs):
        if isinstance(item, dict) and item.get("result") is None:
            return item
    return None


def _collect_effective_bet_chain(state: UserState, include_open: bool = False) -> List[Dict[str, Any]]:
    """
    取出“上一笔赢单之后到当前”的真实下注链。
    - 忽略“异常未结算”等脏记录
    - 可选把最后一条未结算押注也算进当前链路
    """
    logs = _get_strategy_bet_sequence_log(state)
    effective_logs: List[Dict[str, Any]] = []
    open_included = False

    for item in logs:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if result == "异常未结算":
            continue
        if result is None:
            if include_open and not open_included:
                effective_logs.append(item)
                open_included = True
            continue
        effective_logs.append(item)

    chain: List[Dict[str, Any]] = []
    for item in reversed(effective_logs):
        chain.append(item)
        if item.get("result") == "赢" and len(chain) > 1:
            chain.pop()
            break
        if item.get("result") == "赢":
            chain = []
            break
    chain.reverse()
    return chain


def _summarize_effective_bet_chain(state: UserState, include_open: bool = False) -> Dict[str, Any]:
    """根据真实下注链回算连续押注、连输和下一手基准，避免幽灵挂单污染 runtime。"""
    chain = _collect_effective_bet_chain(state, include_open=include_open)
    continuous_count = len(chain)
    lose_count = sum(1 for item in chain if item.get("result") == "输")
    total_losses = sum(
        abs(int(item.get("profit", 0) or 0))
        for item in chain
        if int(item.get("profit", 0) or 0) < 0
    )

    last_amount = 0
    if chain:
        try:
            last_amount = int(chain[-1].get("amount", 0) or 0)
        except Exception:
            last_amount = 0

    start_round = "?"
    start_seq = "?"
    if chain:
        first_bet_id = str(chain[0].get("bet_id", "") or "")
        try:
            if "_" in first_bet_id:
                _, parsed_round, parsed_seq = first_bet_id.split("_")
                start_round, start_seq = parsed_round, parsed_seq
            else:
                nums = re.findall(r"\d+", first_bet_id)
                if len(nums) >= 4:
                    start_round, start_seq = nums[-2], nums[-1]
                else:
                    start_round = chain[0].get("round", "?")
                    start_seq = chain[0].get("sequence", "?")
        except Exception:
            start_round = chain[0].get("round", "?")
            start_seq = chain[0].get("sequence", "?")

    return {
        "chain": chain,
        "continuous_count": continuous_count,
        "lose_count": lose_count,
        "total_losses": total_losses,
        "last_amount": last_amount,
        "start_round": start_round,
        "start_seq": start_seq,
    }


def _summarize_recent_resolved_chain(state: UserState) -> Dict[str, Any]:
    """
    返回“以最近一次真实结算为结尾”的链路。
    例如：输输输赢 -> 返回 4 手；输输输 -> 返回 3 手。
    """
    logs = _get_strategy_bet_sequence_log(state)
    effective_logs: List[Dict[str, Any]] = []
    for item in logs:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if result in (None, "异常未结算"):
            continue
        effective_logs.append(item)

    if not effective_logs:
        chain: List[Dict[str, Any]] = []
    else:
        chain = [effective_logs[-1]]
        for item in reversed(effective_logs[:-1]):
            if item.get("result") == "赢":
                break
            chain.append(item)
        chain.reverse()

    total_losses = sum(
        abs(int(item.get("profit", 0) or 0))
        for item in chain
        if int(item.get("profit", 0) or 0) < 0
    )
    lose_count = sum(1 for item in chain if item.get("result") == "输")

    return {
        "chain": chain,
        "continuous_count": len(chain),
        "lose_count": lose_count,
        "total_losses": total_losses,
    }


def reconcile_bet_runtime_from_log(user_ctx: UserContext, include_open: bool = False) -> Dict[str, Any]:
    """
    用真实下注链回写 runtime。
    这一步专门兜底“重复触发下注导致 sequence 脏掉”的情况。
    """
    state = user_ctx.state
    rt = state.runtime
    summary = _summarize_effective_bet_chain(state, include_open=include_open)
    initial_amount = int(rt.get("initial_amount", 500) or 500)

    rt["bet_sequence_count"] = int(summary["continuous_count"])
    rt["lose_count"] = int(summary["lose_count"])
    rt["bet_amount"] = int(summary["last_amount"] or initial_amount) if summary["continuous_count"] > 0 else initial_amount
    return summary


def _append_bet_sequence_entry(state: UserState, entry: Dict[str, Any]) -> None:
    logs = state.bet_sequence_log if isinstance(state.bet_sequence_log, list) else []
    logs.append(entry)
    state.bet_sequence_log = trim_bet_sequence_log(logs, state.runtime)


def _extract_history_from_bet_on_text(text: str) -> List[int]:
    history_match = re.search(r"\[0\s*小\s*1\s*大\]([\s\S]*)", str(text or ""))
    if not history_match:
        return []
    history_str = history_match.group(1)
    return [int(x) for x in re.findall(r"(?<!\d)[01](?!\d)", history_str)]


def _heal_runtime_open_bet(open_bet_entry: Dict[str, Any], rt: Dict[str, Any]) -> str:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    open_bet_entry["result"] = "异常未结算"
    if open_bet_entry.get("profit") is None:
        open_bet_entry["profit"] = 0
    open_bet_entry["heal_time"] = now_text
    open_bet_entry["heal_note"] = "runtime_auto_heal_missed_settle"
    rt["bet"] = False
    rt["pending_bet_heal_total"] = int(rt.get("pending_bet_heal_total", 0) or 0) + 1
    rt["pending_bet_last_heal_count"] = 1
    rt["pending_bet_last_heal_at"] = now_text
    return str(open_bet_entry.get("bet_id") or "unknown")


def build_pending_bet_heal_notice(healed_pending: Dict[str, Any], summary: Dict[str, Any], rt: Dict[str, Any]) -> str:
    """生成历史脏挂单自愈提示，便于管理员快速确认当前已对齐到哪一手。"""
    healed_count = int(healed_pending.get("count", 0) or 0)
    if healed_count <= 0:
        return ""

    continuous_count = int(summary.get("continuous_count", 0) or 0)
    lose_count = int(summary.get("lose_count", 0) or 0)
    healed_items = healed_pending.get("items", []) if isinstance(healed_pending.get("items"), list) else []

    try:
        next_bet_amount = int(calculate_bet_amount(rt))
    except Exception:
        next_bet_amount = int(rt.get("initial_amount", 0) or 0)

    fixed_text = "、".join(str(item) for item in healed_items[:3]) if healed_items else "已自动修正"
    if len(healed_items) > 3:
        fixed_text += " 等"

    return _build_ops_card(
        "🩹 已修正历史异常挂单",
        summary="检测到历史挂单与当前运行态不一致，系统已自动对齐。",
        fields=[
            ("修复条数", healed_count),
            ("修复记录", fixed_text),
            ("当前连续押注", f"{continuous_count} 次"),
            ("当前连输", f"{lose_count} 次"),
            ("下一手预计下注", format_number(next_bet_amount)),
        ],
        action="建议先执行 `status` 确认当前状态，无需手动重启。",
        note="已按真实已结算记录重新对齐状态。",
    )


def _normalize_ai_keys(ai_cfg: Dict[str, Any]) -> List[str]:
    """统一读取 ai api_keys，兼容旧字段 api_key。"""
    if not isinstance(ai_cfg, dict):
        return []
    raw = ai_cfg.get("api_keys", ai_cfg.get("api_key", []))
    if isinstance(raw, str):
        key = raw.strip()
        return [key] if key else []
    if isinstance(raw, list):
        keys: List[str] = []
        for item in raw:
            text = str(item).strip()
            if text:
                keys.append(text)
        return keys
    return []


def _mask_api_key(key: str) -> str:
    text = str(key or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}***{text[-4:]}"


def _looks_like_ai_key_issue(error_text: str) -> bool:
    text = str(error_text or "").lower()
    if not text:
        return False

    # 明确排除非鉴权问题，避免误判。
    non_auth_signals = ("rate limit", "429", "timeout", "connection", "network")
    if any(sig in text for sig in non_auth_signals):
        return False

    auth_signals = (
        "401",
        "unauthorized",
        "authentication",
        "invalid api key",
        "api key is invalid",
        "invalid token",
        "bad api key",
        "incorrect api key",
        "expired",
        "forbidden",
    )
    return any(sig in text for sig in auth_signals)


def _mark_ai_key_issue(rt: Dict[str, Any], reason: str):
    rt["ai_key_issue_active"] = True
    rt["ai_key_issue_reason"] = str(reason or "")[:200]


def _clear_ai_key_issue(rt: Dict[str, Any]):
    rt["ai_key_issue_active"] = False
    rt["ai_key_issue_reason"] = ""


def _build_ai_key_warning_message(rt: Dict[str, Any]) -> str:
    reason = str(rt.get("ai_key_issue_reason", "")).strip()
    reason_line = f"\n原因：{reason}" if reason else ""
    return (
        f"{AI_KEY_WARNING_TEXT}\n"
        f"当前模型：{rt.get('current_model_id', 'unknown')}{reason_line}\n"
        "请在管理员窗口执行：`apikey set <新key>`"
    )


def get_software_version_text() -> str:
    """返回软件版本展示：tag(hash)。"""
    try:
        info = get_current_repo_info()
        short_commit = info.get("short_commit", "") or "unknown"
        tag = info.get("current_tag", "") or info.get("nearest_tag", "")
        if tag:
            return f"{tag}({short_commit})"
        return short_commit
    except Exception:
        return "unknown"


# 仪表盘格式化 - 与master版本保持一致
def format_dashboard(user_ctx: UserContext) -> str:
    """生成并返回仪表盘信息 - 与master版本format_dashboard一致"""
    state = user_ctx.state
    rt = state.runtime
    
    mes = _build_dashboard_summary(user_ctx)

    reversed_data = ["✅" if x == 1 else "❌" for x in state.history[-40:][::-1]]
    mes += f"""📊 近期 40 次结果（由近及远）
✅：大（1）  ❌：小（0）
{os.linesep.join(
        " ".join(map(str, reversed_data[i:i + 10])) 
        for i in range(0, len(reversed_data), 10)
    )}

———————————————
🎯 策略设定
🔢 软件版本：{get_software_version_text()}
🤖 模型 API：{rt.get('current_model_id', 'unknown')}
🚦 当前押注状态：{get_bet_status_text(rt)}
📋 预设名称：{rt.get('current_preset_name', 'none')}
🤖 预设参数：{rt.get('continuous', 1)} {rt.get('lose_stop', 13)} {rt.get('lose_once', 3.0)} {rt.get('lose_twice', 2.1)} {rt.get('lose_three', 2.05)} {rt.get('lose_four', 2.0)} {rt.get('initial_amount', 500)}
💰 初始金额：{rt.get('initial_amount', 500)}
⏹ 押注 {rt.get('lose_stop', 13)} 次停止
💥 炸 {rt.get('explode', 5)} 次，暂停 {rt.get('stop', 3)} 局
📚 押注倍率：{rt.get('lose_once', 3.0)} / {rt.get('lose_twice', 2.1)} / {rt.get('lose_three', 2.05)} / {rt.get('lose_four', 2.0)}

"""
    
    balance_status = rt.get('balance_status', 'ok')
    account_balance = rt.get('account_balance', 0)
    
    if balance_status == "auth_failed":
        balance_str = "⚠️ Cookie 失效"
    elif balance_status == "network_error":
        balance_str = "⚠️ 网络错误"
    elif account_balance == 0 and balance_status == "unknown":
        balance_str = "⏳ 获取中..."
    else:
        balance_str = f"{account_balance / 10000:.2f} 万"
        
    mes += f"""💰 账户余额：{balance_str}
💰 菠菜余额：{max(0, rt.get('gambling_fund', 0)) / 10000:.2f} 万
📈 盈利目标：{rt.get('profit', 1000000) / 10000:.2f} 万，暂停 {rt.get('profit_stop', 5)} 局
📈 本轮盈利：{rt.get('period_profit', 0) / 10000:.2f} 万
📈 总盈利：{rt.get('earnings', 0) / 10000:.2f} 万

"""
    
    win_total = rt.get('win_total', 0)
    total = rt.get('total', 0)
    if win_total > 0 or total > 0:
        win_rate = (win_total / total * 100) if total > 0 else 0.00
        mes += f"""🎯 押注次数：{total}
🏆 胜率：{win_rate:.2f}%
💰 收益：{format_number(rt.get('earnings', 0))}"""
    
    return mes


def get_bet_status_text(rt: Dict[str, Any]) -> str:
    """统一押注状态展示。"""
    if rt.get("manual_pause", False):
        return "手动暂停"
    if not rt.get("switch", True):
        return "已关闭"

    pause_active = bool(rt.get("pause_countdown_active", False))
    stop_count = max(0, int(rt.get("stop_count", 0) or 0))
    if pause_active or stop_count > 0:
        total_rounds = max(0, int(rt.get("pause_countdown_total_rounds", 0) or 0))
        last_remaining = int(rt.get("pause_countdown_last_remaining", -1) or -1)
        reason = str(rt.get("pause_countdown_reason", "") or "").strip()

        remaining_rounds = 0
        if total_rounds > 0 and 0 < last_remaining <= total_rounds:
            remaining_rounds = last_remaining
        elif total_rounds > 0 and stop_count > 0:
            # 兼容内部 stop_count=暂停局数+1 的实现细节，展示时尽量贴近“真实剩余局数”。
            if stop_count > total_rounds:
                remaining_rounds = total_rounds
            else:
                remaining_rounds = stop_count
        elif stop_count > 0:
            remaining_rounds = max(0, stop_count - 1)

        if remaining_rounds > 0 and reason:
            return f"自动暂停（剩{remaining_rounds}局，{reason}）"
        if remaining_rounds > 0:
            return f"自动暂停（剩{remaining_rounds}局）"
        if reason:
            return f"自动暂停（{reason}）"
        return "自动暂停"

    if rt.get("bet_on", False):
        return "运行中"
    return "已暂停"


def _build_dashboard_summary(user_ctx: UserContext) -> str:
    rt = user_ctx.state.runtime
    status_text = get_bet_status_text(rt)
    preset_name = str(rt.get("current_preset_name", "") or "").strip() or "未设置"
    next_amount = int(calculate_bet_amount(rt) or 0)
    balance_status = rt.get("balance_status", "unknown")
    account_balance = int(rt.get("account_balance", 0) or 0)
    gambling_fund = max(0, int(rt.get("gambling_fund", 0) or 0))

    if balance_status == "auth_failed":
        balance_text = "Cookie 失效"
    elif balance_status == "network_error":
        balance_text = "网络异常"
    elif account_balance <= 0 and balance_status == "unknown":
        balance_text = "获取中"
    else:
        balance_text = f"{account_balance / 10000:.2f} 万"

    summary_lines = [
        "📍 当前概览",
        f"状态：{status_text}",
        f"预设：{preset_name}",
        f"下一手下注：{format_number(next_amount) if next_amount > 0 else '已停止'}",
        f"账户余额：{balance_text}",
        f"菠菜余额：{gambling_fund / 10000:.2f} 万",
        "",
        "———————————————",
        "",
    ]
    return "\n".join(summary_lines)


def _to_bool_switch(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "on", "yes", "y", "enable", "enabled", "开", "开启"}:
            return True
        if normalized in {"0", "false", "off", "no", "n", "disable", "disabled", "关", "关闭"}:
            return False
    return bool(default)


def _risk_switch_label(enabled: bool) -> str:
    return "ON ✅" if enabled else "OFF ⏸"


def _normalize_risk_switches(rt: Dict[str, Any], apply_default: bool = False) -> Dict[str, bool]:
    """
    统一维护风控“当前开关 + 账号默认开关”。
    apply_default=True 时，会把当前开关重置为账号默认值（用于启动恢复）。
    """
    current_base = _to_bool_switch(rt.get("risk_base_enabled", True), True)
    current_deep = _to_bool_switch(rt.get("risk_deep_enabled", True), True)
    default_base = _to_bool_switch(rt.get("risk_base_default_enabled", current_base), current_base)
    default_deep = _to_bool_switch(rt.get("risk_deep_default_enabled", current_deep), current_deep)

    if apply_default:
        current_base = default_base
        current_deep = default_deep

    rt["risk_base_enabled"] = current_base
    rt["risk_deep_enabled"] = current_deep
    rt["risk_base_default_enabled"] = default_base
    rt["risk_deep_default_enabled"] = default_deep

    return {
        "base_enabled": current_base,
        "deep_enabled": current_deep,
        "base_default_enabled": default_base,
        "deep_default_enabled": default_deep,
    }


def apply_account_risk_default_mode(rt: Dict[str, Any]) -> Dict[str, bool]:
    """启动/重启时应用账号默认风控模式。"""
    return _normalize_risk_switches(rt, apply_default=True)


def _build_risk_state_text(rt: Dict[str, Any], include_usage: bool = True) -> str:
    risk_modes = _normalize_risk_switches(rt, apply_default=False)
    mes = (
        f"🛡️🛡️ 当前风控开关 🛡️🛡️\n\n"
        f"- 基础风控：{_risk_switch_label(risk_modes['base_enabled'])}\n"
        f"- 深度风控：{_risk_switch_label(risk_modes['deep_enabled'])}\n\n"
        f"📊 最近 40 笔统计（基础风控依据）\n"
        f"- 胜率：{rt.get('risk_pause_wins', 0)}/{rt.get('risk_pause_total', 40)}（{rt.get('risk_pause_win_rate', 0) * 100:.1f}%）\n"
        f"- 连输档位：{rt.get('risk_deep_milestone', 3)}（已触发 {rt.get('risk_deep_triggered_count', 0)}/{rt.get('risk_deep_trigger_limit', 5)} 次）\n"
    )
    if include_usage:
        mes += "\n用法：`risk base on|off` / `risk deep on|off` / `risk all on|off`"
    return mes

def build_startup_focus_reminder(user_ctx: UserContext) -> str:
    """启动重点设置提醒：风控开关 + 预设 + 入口命令。"""
    rt = user_ctx.state.runtime
    risk_modes = _normalize_risk_switches(rt, apply_default=False)
    preset_name = str(rt.get("current_preset_name", "")).strip() or "未设置"
    try:
        mode_code = int(rt.get("mode", 1) or 1)
    except (TypeError, ValueError):
        mode_code = 1
    mode_text = {0: "反投", 1: "预测", 2: "追投"}.get(mode_code, "未知")
    status_text = get_bet_status_text(rt)
    return (
        "📌 启动重点设置提醒\n"
        f"🛡️ 风控提醒：基础 {_risk_switch_label(risk_modes['base_enabled'])} / "
        f"深度 {_risk_switch_label(risk_modes['deep_enabled'])}\n"
        f"🧭 默认模式：基础 {_risk_switch_label(risk_modes['base_default_enabled'])} / "
        f"深度 {_risk_switch_label(risk_modes['deep_default_enabled'])}（可用 `risk ...` 开关）\n"
        f"🎯 预设提醒：当前 `{preset_name}`（可用 `st <预设名>` 切换）\n"
        f"📊 当前状态：{status_text}，模式：{mode_text}\n"
        "ℹ️ 更多命令：`help`"
    )


# 消息分发规则表（与 master 一致）
MESSAGE_ROUTING_TABLE = {
    "win": {"channels": ["admin", "priority"], "priority": True},
    "explode": {"channels": ["admin", "priority"], "priority": True},
    "lose_streak": {"channels": ["admin", "priority"], "priority": True},
    "lose_end": {"channels": ["admin", "priority"], "priority": True},
    "fund_pause": {"channels": ["admin", "priority"], "priority": True},
    "goal_pause": {"channels": ["admin", "priority"], "priority": True},
    "risk_pause": {"channels": ["admin"], "priority": False},
    "risk_summary": {"channels": ["admin", "priority"], "priority": True},
    "pause": {"channels": ["admin"], "priority": False},
    "resume": {"channels": ["admin"], "priority": False},
    "settle": {"channels": ["admin"], "priority": False},
    "dashboard": {"channels": ["admin"], "priority": False},
    "info": {"channels": ["admin"], "priority": False},
    "warning": {"channels": ["admin"], "priority": False},
    "error": {"channels": ["admin", "priority"], "priority": True},
    "skip_notice": {"channels": ["admin", "priority"], "priority": True},
}

MESSAGE_POLICY = {
    "win": {"level": "P2", "title": "盈利达成", "action": "建议查看 `status`，确认新一轮是否已经开始。"},
    "explode": {"level": "P1", "title": "炸号提醒", "action": "建议立即查看 `status`，必要时调整参数后再继续。"},
    "lose_streak": {"level": "P1", "title": "连输告警", "action": "建议立即查看 `status`，如需止损可执行 `pause`。"},
    "lose_end": {"level": "P2", "title": "连输恢复", "action": "建议关注是否已回到首注，再观察下一次盘口。"},
    "fund_pause": {"level": "P1", "title": "资金暂停", "action": "如需恢复，请执行 `gf 金额` 后再用 `status` 确认。"},
    "goal_pause": {"level": "P1", "title": "目标暂停", "action": "建议等待倒计时结束，或用 `status` 查看剩余暂停局数。"},
    "risk_pause": {"level": "P2", "title": "风控暂停", "action": "建议观察盘面，等待倒计时结束后再继续。"},
    "risk_summary": {"level": "P3", "title": "风控总结", "action": "建议作为复盘信息阅读，不需要立即处理。"},
    "warning": {"level": "P2", "title": "提醒", "action": "建议查看详情，确认是否需要人工介入。"},
    "error": {"level": "P1", "title": "异常提醒", "action": "建议立即查看 `status`，必要时执行 `restart`。"},
}


def _strip_account_prefix(text: str) -> str:
    """管理员消息统一移除账号前缀，与 master 行为一致。"""
    if text is None:
        return ""
    raw = str(text)
    normalized = raw.lstrip()
    if not normalized.startswith("【账号："):
        return raw
    lines = normalized.splitlines()
    if len(lines) <= 1:
        return ""
    return "\n".join(lines[1:]).lstrip("\n")


def _clean_message_lines(text: str) -> List[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _build_ops_card(
    title: str,
    *,
    summary: str = "",
    fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
) -> str:
    lines = [str(title or "").strip()]
    if summary:
        lines.extend(["", f"结论：{summary}"])
    for label, value in fields or []:
        if value in (None, ""):
            continue
        lines.append(f"{label}：{value}")
    if action:
        lines.extend(["", f"建议动作：{action}"])
    if note:
        lines.extend(["", f"补充说明：{note}"])
    return "\n".join(lines).strip()


async def _reply_ops_card(
    event,
    title: str,
    *,
    summary: str = "",
    fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
):
    return await event.reply(
        _build_ops_card(
            title,
            summary=summary,
            fields=fields,
            action=action,
            note=note,
        )
    )


def _build_priority_summary(msg_type: str, text: str, account_prefix: str) -> str:
    content = _strip_account_prefix(text)
    lines = _clean_message_lines(content)
    policy = MESSAGE_POLICY.get(msg_type, {})
    level = policy.get("level", "P2")
    title = policy.get("title", msg_type)
    action = policy.get("action", "")

    summary_lines: List[str] = [account_prefix, f"[{level}] {title}"]
    picked = 0
    for line in lines:
        if picked >= 3:
            break
        summary_lines.append(line)
        picked += 1
    if action:
        summary_lines.append(f"建议：{action}")
    return "\n".join(summary_lines)


def _ensure_account_prefix(text: str, account_prefix: str) -> str:
    """重点渠道消息统一补充账号前缀。"""
    content = _strip_account_prefix(text)
    if not content:
        return account_prefix
    return f"{account_prefix}\n{content}"


def _iter_targets(target):
    if isinstance(target, (list, tuple, set)):
        return [item for item in target if item not in (None, "")]
    if target in (None, ""):
        return []
    return [target]


def _resolve_admin_chat(user_ctx: UserContext):
    notification = user_ctx.config.notification if isinstance(user_ctx.config.notification, dict) else {}
    admin_chat = notification.get("admin_chat")
    if admin_chat in (None, ""):
        admin_chat = user_ctx.config.groups.get("admin_chat")
    if isinstance(admin_chat, str):
        text = admin_chat.strip()
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except Exception:
                return admin_chat
    return admin_chat


def _append_text_record(file_path: str, content: str) -> None:
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(content)


def _cleanup_daily_interaction_files(root_dir: str, retention_days: int = 7) -> None:
    if retention_days <= 0 or not os.path.isdir(root_dir):
        return
    cutoff = datetime.now().date() - timedelta(days=retention_days - 1)
    for entry in os.scandir(root_dir):
        if not entry.is_file() or not entry.name.endswith((".jsonl", ".log")):
            continue
        try:
            stem = entry.name.rsplit(".", 1)[0]
            file_date = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                os.remove(entry.path)
            except OSError:
                pass


def _mask_command_text(command_text: str) -> tuple[str, bool]:
    text = str(command_text or "").strip()
    if not text:
        return "", False
    parts = text.split()
    if not parts:
        return text, False
    normalized_cmd = parts[0][1:] if parts[0].startswith("/") else parts[0]
    cmd = normalized_cmd.lower()
    if cmd in {"apikey", "ak"} and len(parts) >= 2:
        sub_cmd = parts[1].lower()
        if sub_cmd in {"set", "add"} and len(parts) >= 3:
            return " ".join(parts[:2] + ["***"]), True
    return text, False


def _build_interaction_entry(record: Dict[str, Any]) -> str:
    ts = str(record.get("ts", "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    direction = str(record.get("direction", "") or "").strip().lower()
    kind = str(record.get("kind", "") or "").strip().lower()
    channel = str(record.get("channel", "") or "").strip().lower() or "-"
    text = str(record.get("text", "") or "")

    direction_label = "发送" if direction == "outbound" else "接收" if direction == "inbound" else direction or "未知"
    kind_label = "通知" if kind == "notification" else "命令" if kind == "command" else kind or "事件"

    header_parts = [f"[{ts}]", direction_label, channel, kind_label]
    command = str(record.get("command", "") or "").strip()
    msg_type = str(record.get("msg_type", "") or "").strip()
    if command:
        header_parts.append(command)
    elif msg_type:
        header_parts.append(msg_type)

    if "success" in record:
        header_parts.append("成功" if bool(record.get("success")) else "失败")
    if bool(record.get("masked", False)):
        header_parts.append("已脱敏")
    chat_id = record.get("chat_id", None)
    if chat_id not in (None, ""):
        header_parts.append(f"chat_id={chat_id}")
    error = str(record.get("error", "") or "").strip()
    if error:
        header_parts.append(f"error={error[:160]}")

    header = " | ".join(part for part in header_parts if part)
    body = text if text else "(空内容)"
    separator = "─" * 72
    return f"{header}\n{body}\n{separator}\n\n"


def append_interaction_event(
    user_ctx: UserContext,
    *,
    direction: str,
    kind: str,
    channel: str,
    text: str,
    **extra: Any,
) -> None:
    identity = _resolve_account_identity(user_ctx)
    interaction_dir = os.path.join(ACCOUNT_LOG_ROOT, identity["account_slug"], "interactions")
    file_name = datetime.now().strftime("%Y-%m-%d") + ".log"
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": int(identity["user_id"]) if str(identity["user_id"]).isdigit() else identity["user_id"],
        "account_name": identity["account_name"],
        "account_slug": identity["account_slug"],
        "account_label": identity["account_label"],
        "direction": str(direction or "").strip().lower() or "unknown",
        "kind": str(kind or "").strip().lower() or "unknown",
        "channel": str(channel or "").strip().lower() or "unknown",
        "text": str(text or ""),
    }
    for key, value in extra.items():
        if value is None:
            continue
        record[key] = value
    try:
        _append_text_record(os.path.join(interaction_dir, file_name), _build_interaction_entry(record))
        _cleanup_daily_interaction_files(interaction_dir, retention_days=7)
    except Exception as e:
        log_event(logging.WARNING, "interaction", "写入交互审计失败", user_id=user_ctx.user_id, data=str(e))


def _record_outbound_message(
    user_ctx: UserContext,
    *,
    channel: str,
    text: str,
    msg_type: str,
    success: bool,
    parse_mode: Optional[str] = None,
    title: Optional[str] = None,
    chat_id: Any = None,
    error: Optional[str] = None,
) -> None:
    append_interaction_event(
        user_ctx,
        direction="outbound",
        kind="notification",
        channel=channel,
        text=text,
        msg_type=msg_type,
        success=bool(success),
        parse_mode=parse_mode,
        title=title,
        chat_id=chat_id,
        error=error,
    )


async def _post_form_async(url: str, payload: dict, timeout: int = 5):
    """在异步上下文中安全发送 form 请求，避免阻塞事件循环。"""
    return await asyncio.to_thread(requests.post, url, data=payload, timeout=timeout)


async def _post_json_async(url: str, payload: dict, timeout: int = 5):
    """在异步上下文中安全发送 json 请求，避免阻塞事件循环。"""
    return await asyncio.to_thread(requests.post, url, json=payload, timeout=timeout)


async def send_message_v2(
    client,
    msg_type: str,
    message: str,
    user_ctx: UserContext,
    global_config: dict,
    parse_mode: str = "markdown",
    title=None,
    desp=None
):
    """新版统一消息发送函数（多用户版）- 严格按路由表分发。"""
    routing = MESSAGE_ROUTING_TABLE.get(msg_type)
    if routing is None:
        error = f"未定义消息路由: {msg_type}"
        log_event(logging.ERROR, 'send_msg', '消息路由缺失', user_id=user_ctx.user_id, data=error)
        raise ValueError(error)

    channels = routing.get("channels", [])
    account_name = user_ctx.config.name.strip()
    account_prefix = f"【账号：{account_name}】"
    admin_message = _strip_account_prefix(message)
    # 重点通道保留完整详细内容，并统一补充账号前缀，方便多账号并行查看。
    priority_message = _ensure_account_prefix(message, account_prefix)
    priority_desp = _ensure_account_prefix(desp if desp is not None else message, account_prefix)

    sent_message = None
    admin_chat = None
    if "admin" in channels or "all" in channels:
        try:
            admin_chat = _resolve_admin_chat(user_ctx)
            if admin_chat:
                # 修复：多用户分支 - 返回管理员消息对象，确保仪表盘/统计可被后续刷新删除。
                sent_message = await client.send_message(admin_chat, admin_message, parse_mode=parse_mode)
                _record_outbound_message(
                    user_ctx,
                    channel="admin_chat",
                    text=admin_message,
                    msg_type=msg_type,
                    success=True,
                    parse_mode=parse_mode,
                    chat_id=admin_chat,
                )
        except Exception as e:
            log_event(logging.ERROR, 'send_msg', '发送管理员消息失败', user_id=user_ctx.user_id, data=str(e))

    if admin_chat is not None and sent_message is None:
        _record_outbound_message(
            user_ctx,
            channel="admin_chat",
            text=admin_message,
            msg_type=msg_type,
            success=False,
            parse_mode=parse_mode,
            chat_id=admin_chat,
            error="send_failed",
        )

    if "priority" in channels or "all" in channels:
        iyuu_cfg = user_ctx.config.notification.get("iyuu", {})
        if iyuu_cfg.get("enable"):
            try:
                final_title = title or f"菠菜机器人 {account_name} 通知"
                payload = {"text": final_title, "desp": priority_desp}
                iyuu_url = iyuu_cfg.get("url")
                if not iyuu_url:
                    token = iyuu_cfg.get("token")
                    iyuu_url = f"https://iyuu.cn/{token}.send" if token else None
                if iyuu_url:
                    await _post_form_async(iyuu_url, payload, timeout=5)
                    _record_outbound_message(
                        user_ctx,
                        channel="iyuu",
                        text=priority_desp,
                        msg_type=msg_type,
                        success=True,
                        parse_mode=parse_mode,
                        title=final_title,
                    )
            except Exception as e:
                log_event(logging.ERROR, 'send_msg', 'IYUU通知失败', user_id=user_ctx.user_id, data=str(e))

        tg_bot_cfg = user_ctx.config.notification.get("tg_bot", {})
        if tg_bot_cfg.get("enable"):
            try:
                bot_token = tg_bot_cfg.get("bot_token")
                chat_id = tg_bot_cfg.get("chat_id")
                if bot_token and chat_id:
                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    payload = {"chat_id": chat_id, "text": priority_message}
                    await _post_json_async(url, payload, timeout=5)
                    _record_outbound_message(
                        user_ctx,
                        channel="tg_bot",
                        text=priority_message,
                        msg_type=msg_type,
                        success=True,
                        parse_mode=parse_mode,
                        chat_id=chat_id,
                    )
            except Exception as e:
                log_event(logging.ERROR, 'send_msg', 'TG Bot通知失败', user_id=user_ctx.user_id, data=str(e))

    return sent_message


# 兼容旧接口
async def send_message(
    client,
    to: str,
    message: str,
    user_ctx: UserContext,
    global_config: dict,
    parse_mode: str = "markdown",
    title=None,
    desp=None,
    notify_type: str = "info"
):
    msg_type_map = {
        "profit": "win",
        "explode": "explode",
        "lose_streak": "lose_streak",
        "profit_recovery": "lose_end",
        "skip_notice": "skip_notice",
        "info": "info",
    }
    msg_type = msg_type_map.get(notify_type, "info")
    if to not in ("admin", "all", "priority", "iyuu", "tgbot"):
        log_event(logging.WARNING, 'send_msg', '旧接口to参数无效，已按路由表处理', user_id=user_ctx.user_id, data=f"to={to}, type={msg_type}")
        to = "admin"

    if to == "admin":
        return await send_message_v2(client, "info", message, user_ctx, global_config, parse_mode, title, desp)
    if to == "all":
        return await send_message_v2(client, msg_type, message, user_ctx, global_config, parse_mode, title, desp)

    # priority/iyuu/tgbot 兼容：仅走重点渠道
    account_name = user_ctx.config.name.strip()
    account_prefix = f"【账号：{account_name}】"
    priority_message = _ensure_account_prefix(message, account_prefix)
    priority_desp = _ensure_account_prefix(desp if desp is not None else message, account_prefix)
    if to in ("priority", "iyuu"):
        iyuu_cfg = user_ctx.config.notification.get("iyuu", {})
        if iyuu_cfg.get("enable"):
            final_title = title or f"菠菜机器人 {account_name} 通知"
            payload = {"text": final_title, "desp": priority_desp}
            iyuu_url = iyuu_cfg.get("url")
            if not iyuu_url:
                token = iyuu_cfg.get("token")
                iyuu_url = f"https://iyuu.cn/{token}.send" if token else None
            if iyuu_url:
                await _post_form_async(iyuu_url, payload, timeout=5)
                _record_outbound_message(
                    user_ctx,
                    channel="iyuu",
                    text=priority_desp,
                    msg_type=msg_type,
                    success=True,
                    parse_mode=parse_mode,
                    title=final_title,
                )
    if to in ("priority", "tgbot"):
        tg_bot_cfg = user_ctx.config.notification.get("tg_bot", {})
        if tg_bot_cfg.get("enable"):
            bot_token = tg_bot_cfg.get("bot_token")
            chat_id = tg_bot_cfg.get("chat_id")
            if bot_token and chat_id:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {"chat_id": chat_id, "text": priority_message}
                await _post_json_async(url, payload, timeout=5)
                _record_outbound_message(
                    user_ctx,
                    channel="tg_bot",
                    text=priority_message,
                    msg_type=msg_type,
                    success=True,
                    parse_mode=parse_mode,
                    chat_id=chat_id,
                )
    return None


async def send_to_admin(client, message: str, user_ctx: UserContext, global_config: dict):
    return await send_message_v2(client, "info", message, user_ctx, global_config)


async def _send_transient_admin_notice(
    client,
    user_ctx: UserContext,
    global_config: dict,
    message: str,
    ttl_seconds: int = 120,
    attr_name: str = "transient_notice_message",
    msg_type: str = "info",
):
    """
    发送“短时说明通知”（用于暂停结束/恢复等状态提示）：
    - 刷新式保留最后一条
    - 到期自动删除，减少消息堆积
    """
    old_message = getattr(user_ctx, attr_name, None)
    if old_message:
        await cleanup_message(client, old_message)
    sent = await send_to_admin(client, message, user_ctx, global_config)
    if msg_type != "info":
        try:
            await send_message(
                client,
                "priority",
                message,
                user_ctx,
                global_config,
                notify_type=msg_type,
            )
        except Exception:
            pass
    if sent:
        setattr(user_ctx, attr_name, sent)
        chat_id = getattr(sent, "chat_id", None)
        msg_id = getattr(sent, "id", None)
        if chat_id is not None and msg_id is not None and ttl_seconds > 0:
            asyncio.create_task(delete_later(client, chat_id, msg_id, ttl_seconds))
    return sent


# ==================== 核心预测函数 ====================

def calculate_trend_gap(history, window=100):
    """
    计算趋势缺口：最近N期内"大"和"小"偏离50/50均衡线的数值
    返回: {
        'big_ratio': 大占比,
        'small_ratio': 小占比,
        'deviation_score': 标准差/偏离度,
        'gap': 向均值靠拢的缺口(正=缺大, 负=缺小),
        'regression_target': 统计学理论预测目标(0或1)
    }
    """
    if len(history) < window:
        window = len(history)
    
    recent = history[-window:]
    big_count = sum(recent)
    small_count = window - big_count
    
    big_ratio = big_count / window if window > 0 else 0.5
    small_ratio = small_count / window if window > 0 else 0.5
    
    deviation_score = abs(big_ratio - 0.5) * 2
    
    gap = (window / 2) - big_count
    
    regression_target = 1 if big_count < small_count else 0
    
    return {
        'big_ratio': round(big_ratio, 3),
        'small_ratio': round(small_ratio, 3),
        'deviation_score': round(deviation_score, 3),
        'gap': int(gap),
        'regression_target': regression_target,
        'big_count': big_count,
        'small_count': small_count
    }


def extract_pattern_features(history):
    """
    提取形态特征：自动检测单跳、长龙、对称环绕等状态
    返回: {
        'pattern_tag': 形态标签,
        'tail_streak_len': 尾部连龙长度,
        'tail_streak_char': 尾部连龙字符(0/1),
        'is_alternating': 是否单跳模式,
        'is_symmetric': 是否对称环绕
    }
    """
    if not history or len(history) < 3:
        return {
            'pattern_tag': 'INSUFFICIENT_DATA',
            'tail_streak_len': 0,
            'tail_streak_char': None,
            'is_alternating': False,
            'is_symmetric': False
        }
    
    seq_str = ''.join(['1' if x == 1 else '0' for x in history])
    
    tail_char = seq_str[-1]
    tail_streak_len = 1
    for i in range(len(seq_str) - 2, -1, -1):
        if seq_str[i] == tail_char:
            tail_streak_len += 1
        else:
            break
    
    is_alternating = False
    if len(seq_str) >= 6:
        recent_6 = seq_str[-6:]
        if recent_6 in ['010101', '101010']:
            is_alternating = True
    
    is_symmetric = False
    if len(seq_str) >= 5:
        recent_5 = seq_str[-5:]
        if recent_5 == recent_5[::-1]:
            is_symmetric = True
    
    if tail_streak_len >= 4:
        pattern_tag = 'LONG_DRAGON'
    elif tail_streak_len >= 3:
        pattern_tag = 'DRAGON_CANDIDATE'
    elif tail_streak_len == 2:
        pattern_tag = 'DOUBLE_STREAK'
    elif is_alternating:
        pattern_tag = 'SINGLE_JUMP'
    elif is_symmetric:
        pattern_tag = 'SYMMETRIC_WRAP'
    else:
        pattern_tag = 'CHAOS_SWITCH'
    
    return {
        'pattern_tag': pattern_tag,
        'tail_streak_len': tail_streak_len,
        'tail_streak_char': int(tail_char),
        'is_alternating': is_alternating,
        'is_symmetric': is_symmetric
    }


def analyze_double_streak_followups(history, lookback_events: int = 200):
    """
    统计“刚形成2连之后，下一手是继续还是反转”的条件概率。
    只在 streak 首次达到 2 的那个时点计一次，避免把长连拆成多次重复样本。
    """
    if not history or len(history) < 3:
        return {
            "current_side": "",
            "current_side_total": 0,
            "current_continue": 0,
            "current_reverse": 0,
            "current_continue_rate": 0.0,
            "current_reverse_rate": 0.0,
            "current_preference": "neutral",
        }

    events = []
    streak_len = 1
    for i in range(1, len(history) - 1):
        if history[i] == history[i - 1]:
            streak_len += 1
        else:
            streak_len = 1
        if streak_len == 2:
            side = int(history[i])
            next_value = int(history[i + 1])
            events.append({
                "side": side,
                "continue": next_value == side,
            })

    if lookback_events > 0:
        events = events[-lookback_events:]

    tail_side = int(history[-1])
    current_side_text = "big" if tail_side == 1 else "small"
    side_events = [event for event in events if event["side"] == tail_side]
    total = len(side_events)
    continue_count = sum(1 for event in side_events if event["continue"])
    reverse_count = total - continue_count
    continue_rate = round((continue_count / total), 3) if total > 0 else 0.0
    reverse_rate = round((reverse_count / total), 3) if total > 0 else 0.0

    preference = "neutral"
    if total >= 8:
        if continue_rate - reverse_rate >= 0.08:
            preference = "continue"
        elif reverse_rate - continue_rate >= 0.08:
            preference = "reverse"

    return {
        "current_side": current_side_text,
        "current_side_total": total,
        "current_continue": continue_count,
        "current_reverse": reverse_count,
        "current_continue_rate": continue_rate,
        "current_reverse_rate": reverse_rate,
        "current_preference": preference,
    }


def _best_repeating_pattern_match(seq_str: str, patterns: List[str]) -> Dict[str, Any]:
    """在候选重复节奏里找出最匹配的模板，并推导下一手期望值。"""
    if not seq_str:
        return {"pattern": "", "score": 0.0, "next_char": None}

    best_pattern = ""
    best_score = -1.0
    best_next_char = None
    for pattern in patterns:
        expanded = (pattern * ((len(seq_str) // len(pattern)) + 2))[:len(seq_str)]
        score = sum(1 for current, expected in zip(seq_str, expanded) if current == expected) / len(seq_str)
        if score > best_score:
            best_score = score
            best_pattern = pattern
            best_next_char = pattern[len(seq_str) % len(pattern)]

    return {
        "pattern": best_pattern,
        "score": round(best_score, 3),
        "next_char": int(best_next_char) if best_next_char is not None else None,
    }


def analyze_rhythm_context(history, recent_window: int = 9, lookback_events: int = 200):
    """
    识别当前更像交替节奏、配对节奏、长龙还是混沌，并结合历史窗口统计命中率。
    配对节奏不是简单看 2 连，而是看最近序列更像 101/110/001/010 这类“补成二连”的重复节奏。
    """
    if not history or len(history) < 4:
        return {
            "recent_seq": "",
            "rhythm_tag": "CHAOS_NOISE",
            "alternation_score": 0.0,
            "alternation_pattern": "",
            "alternation_next": None,
            "alternation_hit_rate": 0.0,
            "alternation_samples": 0,
            "pair_score": 0.0,
            "pair_pattern": "",
            "pair_next": None,
            "pair_hit_rate": 0.0,
            "pair_samples": 0,
            "dragon_score": 0.0,
            "chaos_score": 1.0,
            "pair_would_form_double": False,
            "pair_would_chase_triple": False,
        }

    recent_len = min(max(4, int(recent_window)), len(history))
    recent_seq = "".join("1" if x == 1 else "0" for x in history[-recent_len:])

    alternation_patterns = ["01", "10"]
    pair_patterns = ["001", "010", "100", "110", "101", "011"]
    alternation_match = _best_repeating_pattern_match(recent_seq, alternation_patterns)
    pair_match = _best_repeating_pattern_match(recent_seq, pair_patterns)

    tail_streak_len = 1
    tail_value = history[-1]
    for value in reversed(history[:-1]):
        if value == tail_value:
            tail_streak_len += 1
        else:
            break

    dragon_score = round(min(tail_streak_len / 4.0, 1.0), 3) if tail_streak_len >= 2 else 0.0
    pair_would_form_double = tail_streak_len == 1 and pair_match["next_char"] == tail_value
    pair_would_chase_triple = tail_streak_len >= 2 and pair_match["next_char"] == tail_value

    alternation_samples = 0
    alternation_hits = 0
    pair_samples = 0
    pair_hits = 0
    start_idx = max(recent_len, len(history) - max(int(lookback_events), recent_len))
    for idx in range(start_idx, len(history)):
        prior = history[idx - recent_len:idx]
        if len(prior) < recent_len:
            continue
        prior_seq = "".join("1" if x == 1 else "0" for x in prior)
        actual_next = int(history[idx])
        prior_alt = _best_repeating_pattern_match(prior_seq, alternation_patterns)
        prior_pair = _best_repeating_pattern_match(prior_seq, pair_patterns)

        if prior_alt["score"] >= 0.75:
            alternation_samples += 1
            if prior_alt["next_char"] == actual_next:
                alternation_hits += 1
        if prior_pair["score"] >= 0.67:
            pair_samples += 1
            if prior_pair["next_char"] == actual_next:
                pair_hits += 1

    alternation_hit_rate = round(alternation_hits / alternation_samples, 3) if alternation_samples else 0.0
    pair_hit_rate = round(pair_hits / pair_samples, 3) if pair_samples else 0.0

    alternation_edge = alternation_match["score"] * max(alternation_hit_rate, 0.45)
    pair_edge = pair_match["score"] * max(pair_hit_rate, 0.45)
    if dragon_score >= 1.0 and dragon_score > alternation_match["score"] + 0.08 and dragon_score > pair_match["score"] + 0.08:
        rhythm_tag = "DRAGON_TREND"
    elif alternation_match["score"] >= 0.78 and alternation_edge > pair_edge + 0.06:
        rhythm_tag = "ALTERNATION_RHYTHM"
    elif pair_match["score"] >= 0.67 and pair_edge > alternation_edge + 0.04:
        rhythm_tag = "PAIR_FORMATION"
    else:
        rhythm_tag = "CHAOS_NOISE"

    chaos_score = round(max(0.0, 1.0 - max(alternation_match["score"], pair_match["score"], dragon_score)), 3)

    return {
        "recent_seq": recent_seq,
        "rhythm_tag": rhythm_tag,
        "alternation_score": alternation_match["score"],
        "alternation_pattern": alternation_match["pattern"],
        "alternation_next": alternation_match["next_char"],
        "alternation_hit_rate": alternation_hit_rate,
        "alternation_samples": alternation_samples,
        "pair_score": pair_match["score"],
        "pair_pattern": pair_match["pattern"],
        "pair_next": pair_match["next_char"],
        "pair_hit_rate": pair_hit_rate,
        "pair_samples": pair_samples,
        "dragon_score": dragon_score,
        "chaos_score": chaos_score,
        "pair_would_form_double": pair_would_form_double,
        "pair_would_chase_triple": pair_would_chase_triple,
    }


def fallback_prediction(history):
    """
    统计兜底预测。
    当模型不可用时，优先补最近窗口里相对偏少的一侧。
    """
    if not history:
        return 1
    
    window = min(40, len(history))
    recent = history[-window:]
    big_count = sum(recent)
    small_count = window - big_count
    
    prediction = 1 if big_count < small_count else 0
    
    log_event(logging.WARNING, 'predict_core', '统计兜底触发', 
              user_id=0, data=f'big={big_count}, small={small_count}, fallback={prediction}')
    
    return prediction


def parse_analysis_result_insight(resp_text, default_prediction=1):
    """
    解析 AI 输出，返回 prediction/confidence/reason。
    prediction 允许: 1(大) / 0(小) / -1(SKIP)
    """
    try:
        cleaned = str(resp_text).replace('```json', '').replace('```', '').strip()
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].strip()
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]
        resp_json = json.loads(cleaned)
        
        prediction = resp_json.get('prediction', default_prediction)
        if isinstance(prediction, str):
            pred_norm = prediction.strip().upper()
            if pred_norm in {'-1', 'SKIP', 'NONE', 'PASS', 'WAIT', '观望', '跳过'}:
                prediction = -1
            elif pred_norm in {'1', 'B', 'BIG', '大'}:
                prediction = 1
            elif pred_norm in {'0', 'S', 'SMALL', '小'}:
                prediction = 0
            else:
                prediction = default_prediction
        try:
            prediction = int(prediction)
        except Exception:
            prediction = default_prediction
        if prediction not in [-1, 0, 1]:
            prediction = default_prediction
        
        confidence = int(resp_json.get('confidence', 50))
        confidence = max(0, min(100, confidence))
        
        reason = resp_json.get('reason', resp_json.get('logic', '模型分析'))
        
        return {
            'prediction': prediction,
            'confidence': confidence,
            'reason': reason
        }
    except Exception as e:
        return {
            'prediction': default_prediction,
            'confidence': 50,
            'reason': f'解析兜底:{str(e)[:20]}'
        }


# 主预测函数
async def predict_next_bet_core(user_ctx: UserContext, global_config: dict, current_round: int = 1) -> int:
    """
    根据历史节奏、统计特征和模型输出来决定本局方向。
    """
    state = user_ctx.state
    rt = state.runtime
    history = state.history
    
    try:
        # 第一步：构建历史窗口快照。
        
        # 短期窗口（20局）
        short_term_20 = history[-20:] if len(history) >= 20 else history[:]
        short_str = "".join(['1' if x == 1 else '0' for x in short_term_20])
        
        # 中期窗口（50局）
        medium_term_50 = history[-50:] if len(history) >= 50 else history[:]
        medium_str = "".join(['1' if x == 1 else '0' for x in medium_term_50])
        
        # 长期窗口（100局）
        long_term_100 = history[-100:] if len(history) >= 100 else history[:]
        long_term_gap = round(sum(long_term_100) / len(long_term_100), 3) if long_term_100 else 0.5
        
        # 趋势缺口
        trend_gap = calculate_trend_gap(history, window=100)
        big_cnt = trend_gap['big_count']
        small_cnt = trend_gap['small_count']
        gap = trend_gap['gap']
        
        # 形态与节奏特征
        pattern_features = extract_pattern_features(history)
        pattern_tag = pattern_features['pattern_tag']
        tail_streak_len = pattern_features['tail_streak_len']
        tail_streak_char = pattern_features['tail_streak_char']
        double_streak_stats = analyze_double_streak_followups(history)
        rhythm_context = analyze_rhythm_context(history)
        
        # 当前连押压力标签
        lose_count = rt.get('lose_count', 0)
        entropy_tag = "Pattern_Breaking" if lose_count > 2 else "Stability"
        
        # 第二步：整理模型输入上下文。
        
        payload = {
            "current_status": {
                "martingale_step": lose_count + 1,
                "total_profit_to_date": rt.get('earnings', 0),
                "entropy_tag": entropy_tag
            },
            "history_views": {
                "short_term_20": short_str,
                "medium_term_50": medium_str,
                "long_term_gap": long_term_gap,
                "big_count_100": big_cnt,
                "small_count_100": small_cnt
            },
            "pattern_analysis": {
                "tag": pattern_tag,
                "tail_streak_len": tail_streak_len,
                "tail_streak_char": tail_streak_char,
                "gap": f"{gap:+d}"
            },
            "double_streak_analysis": {
                "current_side": double_streak_stats["current_side"],
                "sample_count": double_streak_stats["current_side_total"],
                "continue_count": double_streak_stats["current_continue"],
                "reverse_count": double_streak_stats["current_reverse"],
                "continue_rate": double_streak_stats["current_continue_rate"],
                "reverse_rate": double_streak_stats["current_reverse_rate"],
                "preference": double_streak_stats["current_preference"],
            },
            "rhythm_analysis": {
                "tag": rhythm_context["rhythm_tag"],
                "recent_seq": rhythm_context["recent_seq"],
                "alternation_score": rhythm_context["alternation_score"],
                "alternation_pattern": rhythm_context["alternation_pattern"],
                "alternation_next": rhythm_context["alternation_next"],
                "alternation_hit_rate": rhythm_context["alternation_hit_rate"],
                "alternation_samples": rhythm_context["alternation_samples"],
                "pair_score": rhythm_context["pair_score"],
                "pair_pattern": rhythm_context["pair_pattern"],
                "pair_next": rhythm_context["pair_next"],
                "pair_hit_rate": rhythm_context["pair_hit_rate"],
                "pair_samples": rhythm_context["pair_samples"],
                "dragon_score": rhythm_context["dragon_score"],
                "chaos_score": rhythm_context["chaos_score"],
                "pair_would_form_double": rhythm_context["pair_would_form_double"],
                "pair_would_chase_triple": rhythm_context["pair_would_chase_triple"],
            }
        }
        
        # 第三步：构建推理提示词。
        
        current_model_id = rt.get('current_model_id', 'qwen3-coder-plus')
        actual_model_id = current_model_id
        prompt = f"""[System Instruction]
You are a quantitative trading analyst for a binary big/small game. First identify the dominant rhythm of the board, then decide whether it deserves a bet. If evidence is weak or conflicting, output SKIP (-1).

[Pattern Priority]
1. LONG_DRAGON: tail streak >= 4. This is now a mature dragon pattern.
2. DRAGON_CANDIDATE: tail streak == 3.
3. DOUBLE_STREAK: tail streak == 2. This is useful, but it is not enough by itself.
4. Rhythm layer: alternation rhythm vs pair formation rhythm.
5. SINGLE_JUMP / SYMMETRIC_WRAP / CHAOS_SWITCH are weaker transition structures.

[Rhythm Layer]
- rhythm_tag: {rhythm_context['rhythm_tag']}
- recent_seq: {rhythm_context['recent_seq']}
- alternation_score: {rhythm_context['alternation_score']:.3f}
- alternation_pattern: {rhythm_context['alternation_pattern']}
- alternation_expected_next: {rhythm_context['alternation_next']}
- alternation_hit_rate: {rhythm_context['alternation_hit_rate']:.3f} (samples={rhythm_context['alternation_samples']})
- pair_score: {rhythm_context['pair_score']:.3f}
- pair_pattern: {rhythm_context['pair_pattern']}
- pair_expected_next: {rhythm_context['pair_next']}
- pair_hit_rate: {rhythm_context['pair_hit_rate']:.3f} (samples={rhythm_context['pair_samples']})
- pair_would_form_double: {str(rhythm_context['pair_would_form_double']).lower()}
- pair_would_chase_triple: {str(rhythm_context['pair_would_chase_triple']).lower()}
- dragon_score: {rhythm_context['dragon_score']:.3f}
- chaos_score: {rhythm_context['chaos_score']:.3f}

[Rhythm Rules]
1. If alternation_score is clearly stronger than pair_score and the history hit rate also supports it, treat the board as ALTERNATION_RHYTHM. Follow alternation_expected_next instead of guessing that alternation will suddenly break.
2. If pair_score is clearly stronger than alternation_score and the history hit rate supports it, treat the board as PAIR_FORMATION. Favor pair_expected_next only when it is trying to form the next double.
3. If pair_would_chase_triple is true, reduce confidence sharply. Pair logic is mainly for forming the next 2-streak, not for aggressively chasing 3-streak.
4. If recent_seq is a long pure alternation chain and no real double has appeared yet, be very cautious about betting against alternation. Pair bets need clearly better evidence.
5. If alternation_score and pair_score are close, or rhythm_tag is CHAOS_NOISE, prefer SKIP.

[Double Streak Rule]
- side: {double_streak_stats['current_side']}
- sample_count: {double_streak_stats['current_side_total']}
- continue_count: {double_streak_stats['current_continue']}
- reverse_count: {double_streak_stats['current_reverse']}
- continue_rate: {double_streak_stats['current_continue_rate']:.3f}
- reverse_rate: {double_streak_stats['current_reverse_rate']:.3f}
- preference: {double_streak_stats['current_preference']}
Interpretation:
- DOUBLE_STREAK is a supporting clue, not the only clue.
- If pair rhythm is strong and the next hand would form a fresh double, DOUBLE_STREAK can be weighted higher.
- If DOUBLE_STREAK already exists and the next hand would directly chase a triple, lower its weight.

[Hard Risk Rules]
1. If martingale_step >= {HIGH_PRESSURE_SKIP_MIN_STEP} and confidence < {HIGH_PRESSURE_SKIP_MIN_CONF}, output SKIP.
2. If pattern tag is CHAOS_SWITCH / SINGLE_JUMP / SYMMETRIC_WRAP and martingale_step >= 3, default to SKIP.
3. DRAGON_CANDIDATE is not enough by itself in high-pressure hands.
4. If long_term_gap is near neutral [{NEUTRAL_LONG_TERM_GAP_LOW:.2f}, {NEUTRAL_LONG_TERM_GAP_HIGH:.2f}], do not use long-term distribution as a strong betting reason.
5. If trend evidence and reversal evidence conflict, output SKIP.

[Data Evidence]
short_term_20: {short_str}
medium_term_50: {medium_str}
long_term_big_ratio: {long_term_gap:.2f}
pattern_tag: {pattern_tag}
tail_streak_len: {tail_streak_len}
tail_side: {'big' if tail_streak_char == 1 else 'small'}
gap: {gap:+d}
martingale_step: {lose_count + 1}
entropy_tag: {entropy_tag}

[Output Policy]
- Decide the dominant board rhythm first: dragon / alternation / pair / chaos.
- If alternation rhythm dominates, prefer the alternation continuation side.
- If pair rhythm dominates, prefer the side that forms the next double.
- If neither rhythm is clearly dominant, or if the dominant rhythm is unsupported by history hit rate, output SKIP.

[Response Format]
Return JSON only:
{{"logic": "short summary", "reasoning": "why bet or skip", "confidence": 1-100, "prediction": -1 or 0 or 1}}"""

        messages = [
            {'role': 'system', 'content': '你是专门破解博弈陷阱的量化交易员，只输出纯JSON。prediction 仅允许 -1/0/1。'},
            {'role': 'user', 'content': prompt}
        ]
        
        log_event(logging.INFO, 'predict_core', f'模型分析调用: {current_model_id}', 
                  user_id=user_ctx.user_id, data=f'形态:{pattern_tag} 缺口:{gap:+d} 压力:{lose_count + 1}次')
        
        # 第四步：调用模型并处理降级。

        model_used = True
        try:
            configured_keys = _normalize_ai_keys(user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {})
            if not configured_keys:
                raise Exception("AI_KEY_MISSING")

            result = await user_ctx.get_model_manager().call_model(
                current_model_id,
                messages,
                temperature=0.1,
                max_tokens=500
            )
            if not result['success']:
                raise Exception(f"Model Error: {result['error']}")

            _clear_ai_key_issue(rt)
            actual_model_id = str(result.get("model_id") or current_model_id)
            if actual_model_id != current_model_id:
                rt["current_model_id"] = actual_model_id
                log_event(
                    logging.WARNING,
                    'predict_core',
                    '主模型不可用，已按排序自动降级',
                    user_id=user_ctx.user_id,
                    data=f'{current_model_id} -> {actual_model_id}'
                )
                user_ctx.save_state()
                current_model_id = actual_model_id
            
            default_pred = trend_gap['regression_target']
            final_result = parse_analysis_result_insight(result['content'], default_prediction=default_pred)
            
        except Exception as model_error:
            model_used = False
            err_text = str(model_error)
            if "AI_KEY_MISSING" in err_text:
                _mark_ai_key_issue(rt, "未配置可用 api_keys")
            elif _looks_like_ai_key_issue(err_text):
                _mark_ai_key_issue(rt, err_text)
            log_event(logging.WARNING, 'predict_core', '模型调用失败，统计兜底', 
                      user_id=user_ctx.user_id, data=err_text)
            final_result = {
                'prediction': trend_gap['regression_target'],
                'confidence': 50,
                'reason': '模型异常，统计回归兜底'
            }
        
        # 第五步：校验输出并写回运行态。
        
        prediction = final_result['prediction']
        confidence = final_result['confidence']
        reason = final_result.get('reason', final_result.get('logic', '深度分析'))
        
        if prediction not in [-1, 0, 1]:
            prediction = trend_gap['regression_target']
            confidence = 50
            reason = '强制校正：统计回归'
        
        # 构建预测信息
        rt["last_predict_info"] = (
            f"模型分析/{pattern_tag}/{rhythm_context['rhythm_tag']} | {reason} | 信:{confidence}% | "
            f"缺口:{gap:+d} | 回归:{trend_gap['regression_target']}"
        )
        rt["last_predict_tag"] = pattern_tag
        rt["last_predict_confidence"] = int(confidence)
        if prediction == -1:
            rt["last_predict_source"] = "model_skip" if model_used else "fallback_skip"
        else:
            rt["last_predict_source"] = "model" if model_used else "fallback"
        rt["last_predict_reason"] = reason
        rt["last_predict_gap"] = int(gap)
        rt["last_predict_long_term_gap"] = float(long_term_gap)
        rt["last_predict_tail_len"] = int(tail_streak_len)
        rt["last_predict_tail_char"] = int(tail_streak_char)
        
        # 审计日志
        audit_log = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "round": current_round,
            "mode": "core_predictor",
            "input_payload": payload,
            "output": final_result,
            "model_id": actual_model_id,
            "prediction_source": rt.get("last_predict_source", "unknown"),
            "pattern_tag": pattern_tag,
        }
        rt["last_logic_audit"] = json.dumps(audit_log, ensure_ascii=False, indent=2)
        
        # 记录预测
        state.predictions.append(prediction)
        
        log_event(logging.INFO, 'predict_core', '模型分析完成', 
                  user_id=user_ctx.user_id, data=f'pred={prediction}, conf={confidence}, pattern={pattern_tag}')
        
        return prediction
        
    except Exception as e:
        log_event(logging.ERROR, 'predict_core', '核心预测异常，使用最终保底', 
                  user_id=user_ctx.user_id, data=str(e))
        
        recent_20 = history[-20:] if len(history) >= 20 else history
        recent_sum = sum(recent_20)
        fallback = 0 if recent_sum >= len(recent_20) / 2 else 1
        
        rt["last_predict_info"] = f"模型最终兜底 | 强制预测:{fallback}"
        rt["last_predict_tag"] = "FALLBACK"
        rt["last_predict_confidence"] = 0
        rt["last_predict_source"] = "hard_fallback"
        rt["last_predict_reason"] = "模型异常最终兜底"
        state.predictions.append(fallback)
        return fallback


# 押注处理
async def _refresh_dashboard_message_slim(client, user_ctx: UserContext, global_config: dict):
    dashboard = format_dashboard(user_ctx)
    if hasattr(user_ctx, "dashboard_message") and user_ctx.dashboard_message:
        await cleanup_message(client, user_ctx.dashboard_message)
    user_ctx.dashboard_message = await send_to_admin(client, dashboard, user_ctx, global_config)
    return user_ctx.dashboard_message


async def _process_bet_on_slim(client, event, user_ctx: UserContext, global_config: dict):
    state = user_ctx.state
    rt = state.runtime

    timing_cfg = _read_timing_config(global_config)
    prompt_wait_sec = timing_cfg["prompt_wait_sec"]
    predict_timeout_sec = timing_cfg["predict_timeout_sec"]
    click_interval_sec = timing_cfg["click_interval_sec"]
    click_timeout_sec = timing_cfg["click_timeout_sec"]

    if not getattr(event, "reply_markup", None) and prompt_wait_sec > 0:
        await asyncio.sleep(prompt_wait_sec)

    text = event.message.message
    history_before = list(state.history)
    incoming_history: List[int] = []
    try:
        incoming_history = _extract_history_from_bet_on_text(text)
        if incoming_history and len(incoming_history) >= len(history_before):
            state.history = incoming_history[-2000:]
    except Exception as e:
        log_event(logging.WARNING, 'bet_on', '解析历史数据失败', user_id=user_ctx.user_id, data=str(e))

    if not rt.get("switch", True):
        if rt.get("bet", False):
            rt["bet"] = False
            user_ctx.save_state()
        return

    if rt.get("manual_pause", False):
        if rt.get("bet", False):
            rt["bet"] = False
            user_ctx.save_state()
        return

    open_bet_entry = _get_latest_open_bet_entry(state)
    if rt.get("bet", False) and open_bet_entry is not None:
        history_advanced = False
        if incoming_history:
            previous_history_tail = history_before[-len(incoming_history):] if len(history_before) >= len(incoming_history) else history_before[:]
            history_advanced = incoming_history != previous_history_tail
        if history_advanced:
            healed_bet_id = _heal_runtime_open_bet(open_bet_entry, rt)
            summary = reconcile_bet_runtime_from_log(user_ctx)
            user_ctx.save_state()
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                _build_ops_card(
                    "🩹 运行中已修正异常挂单",
                    summary="检测到上一手疑似漏结算，系统已按新一轮历史自动对齐后继续处理。",
                    fields=[
                        ("修复记录", healed_bet_id),
                        ("当前连续押注", f"{summary.get('continuous_count', 0)} 次"),
                        ("当前连输", f"{summary.get('lose_count', 0)} 次"),
                    ],
                    action="建议执行 `status` 确认当前链路；本局会继续尝试正常下注。",
                ),
                ttl_seconds=180,
                attr_name="pending_bet_heal_message",
                msg_type="skip_notice",
            )
        else:
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                _build_ops_card(
                    "⏳ 上一手仍待结算",
                    summary="当前检测到上一手还未完成结算，系统不会重复下注。",
                    fields=[("待结算记录", str(open_bet_entry.get("bet_id", "unknown")))],
                    action="建议等待结果回写；若长时间不更新，可执行 `status` 检查。",
                ),
                ttl_seconds=90,
                attr_name="pending_bet_hold_message",
                msg_type="skip_notice",
            )
            return
    if rt.get("bet", False) and open_bet_entry is None:
        rt["bet"] = False
        user_ctx.save_state()

    healed_pending = heal_stale_pending_bets(user_ctx)
    if healed_pending.get("count", 0) > 0:
        summary = reconcile_bet_runtime_from_log(user_ctx)
        user_ctx.save_state()
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            build_pending_bet_heal_notice(healed_pending, summary, rt),
            ttl_seconds=180,
            attr_name="pending_bet_heal_message",
        )

    stop_count = int(rt.get("stop_count", 0) or 0)
    if stop_count > 0:
        rt["stop_count"] = max(0, stop_count - 1)
        rt["bet"] = False
        if rt["stop_count"] == 0:
            rt["bet_on"] = True
            rt["mode_stop"] = True
            rt["pause_reason"] = ""
        user_ctx.save_state()
        return

    bet_amount = calculate_bet_amount(rt)
    if bet_amount <= 0:
        if not rt.get("limit_stop_notified", False):
            lose_stop = int(rt.get("lose_stop", 13))
            mes = (
                "⚠️ 已达到预设连投上限，已自动暂停\n"
                f"当前预设最多连投：{lose_stop} 手\n"
                "可等待新轮次或切换预设后继续"
            )
            await send_to_admin(client, mes, user_ctx, global_config)
            rt["limit_stop_notified"] = True
        rt["bet"] = False
        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        user_ctx.save_state()
        return
    rt["limit_stop_notified"] = False

    if not is_fund_available(user_ctx, bet_amount):
        if _sync_fund_from_account_when_insufficient(rt, bet_amount):
            user_ctx.save_state()
        if not is_fund_available(user_ctx, bet_amount):
            if not rt.get("fund_pause_notified", False):
                display_fund = max(0, rt.get("gambling_fund", 0))
                mes = _build_fund_pause_message(display_fund)
                await send_message_v2(
                    client,
                    "fund_pause",
                    mes,
                    user_ctx,
                    global_config,
                    title=f"菠菜机器人 {user_ctx.config.name} 资金暂停",
                    desp=mes,
                )
                rt["fund_pause_notified"] = True
            rt["bet"] = False
            rt["bet_on"] = False
            rt["mode_stop"] = True
            _clear_lose_recovery_tracking(rt)
            user_ctx.save_state()
            return
    rt["fund_pause_notified"] = False

    if not (rt.get("bet_on", False) or rt.get("mode_stop", True)):
        return

    if not event.reply_markup:
        rt["bet"] = False
        user_ctx.save_state()
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            _build_ops_card(
                "⏭️ 本局未执行下注",
                summary="当前盘口消息没有可点击按钮，系统已自动跳过本局。",
                action="建议等待下一次盘口；如频繁出现，请检查群消息格式。",
            ),
            ttl_seconds=90,
            attr_name="skip_reason_message",
            msg_type="skip_notice",
        )
        return

    try:
        prediction = await asyncio.wait_for(
            predict_next_bet_core(user_ctx, global_config),
            timeout=predict_timeout_sec,
        )
    except asyncio.TimeoutError:
        prediction = int(fallback_prediction(state.history))
        rt["last_predict_info"] = f"预测超时，改用统计兜底（{'大' if prediction == 1 else '小'}）"
        rt["last_predict_source"] = "timeout_fallback"
        rt["last_predict_tag"] = "TIMEOUT_FALLBACK"
        rt["last_predict_confidence"] = 0

    if prediction not in (-1, 0, 1):
        prediction = int(fallback_prediction(state.history))
        rt["last_predict_info"] = f"预测无效，改用统计兜底（{'大' if prediction == 1 else '小'}）"
        rt["last_predict_source"] = "invalid_fallback"
        rt["last_predict_tag"] = "INVALID_FALLBACK"
        rt["last_predict_confidence"] = 0

    if prediction == -1:
        rt["bet"] = False
        rt["bet_on"] = True
        rt["mode_stop"] = True
        user_ctx.save_state()
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            _build_ops_card(
                "⏭️ 本局策略选择观望",
                summary="当前模型判断不建议下注，本局已主动跳过。",
                fields=[("原因", rt.get("last_predict_info", "模型未给出可执行信号"))],
                action="建议继续观察下一次盘口，或执行 `status` 查看当前状态。",
            ),
            ttl_seconds=120,
            attr_name="skip_reason_message",
            msg_type="skip_notice",
        )
        return

    if rt.get("ai_key_issue_active", False):
        await send_to_admin(client, _build_ai_key_warning_message(rt), user_ctx, global_config)

    rt["bet_amount"] = int(bet_amount)
    direction = "大" if prediction == 1 else "小"
    direction_en = "big" if prediction == 1 else "small"
    buttons = constants.BIG_BUTTON if prediction == 1 else constants.SMALL_BUTTON
    combination = constants.find_combination(rt["bet_amount"], buttons)

    if not combination:
        rt["bet"] = False
        user_ctx.save_state()
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            _build_ops_card(
                "⏭️ 本局未执行下注",
                summary="当前金额没有匹配到可点击的下注按钮组合。",
                fields=[("目标金额", format_number(rt["bet_amount"]))],
                action="建议检查按钮映射配置，或等待下一次盘口。",
            ),
            ttl_seconds=120,
            attr_name="skip_reason_message",
            msg_type="skip_notice",
        )
        return

    try:
        for amount in combination:
            button_data = buttons.get(amount)
            if button_data is not None:
                await asyncio.wait_for(
                    _click_bet_button_with_recover(client, event, user_ctx, button_data),
                    timeout=click_timeout_sec,
                )
                await asyncio.sleep(click_interval_sec)
    except Exception as e:
        if _is_invalid_callback_message_error(e):
            await send_to_admin(
                client,
                _build_ops_card(
                    "⏰ 本轮下注窗口已失效",
                    summary="当前盘口的按钮已经不可用，系统已自动跳过本局。",
                    action="无需手动补单，建议等待下一次盘口并关注结果通知。",
                ),
                user_ctx,
                global_config,
            )
        else:
            await send_to_admin(
                client,
                _build_ops_card(
                    "❌ 押注执行失败",
                    summary="本次下注没有执行成功。",
                    fields=[("错误", str(e)[:180])],
                    action="建议先执行 `status` 查看当前状态，再决定是否继续。",
                ),
                user_ctx,
                global_config,
            )
        return

    rt["bet"] = True
    rt["total"] = rt.get("total", 0) + 1
    rt["bet_sequence_count"] = rt.get("bet_sequence_count", 0) + 1
    rt["bet_type"] = 1 if prediction == 1 else 0
    rt["bet_on"] = True
    rt["fund_pause_notified"] = False
    rt["limit_stop_notified"] = False

    bet_id = generate_bet_id(user_ctx)
    _append_bet_sequence_entry(state, {
        "bet_id": bet_id,
        "sequence": rt.get("bet_sequence_count", 0),
        "direction": direction_en,
        "amount": rt["bet_amount"],
        "result": None,
        "profit": 0,
        "lose_stop": rt.get("lose_stop", 13),
        "profit_target": rt.get("profit", 1000000)
    })

    bet_report = generate_mobile_bet_report(
        state.history,
        direction,
        rt["bet_amount"],
        rt.get("bet_sequence_count", 1),
        bet_id
    )
    message = await send_to_admin(client, bet_report, user_ctx, global_config)
    asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
    if message:
        asyncio.create_task(delete_later(client, message.chat_id, message.id, 100))

    await _refresh_dashboard_message_slim(client, user_ctx, global_config)

    rt["current_bet_seq"] = int(rt.get("current_bet_seq", 1)) + 1
    user_ctx.save_state()


async def process_bet_on(client, event, user_ctx: UserContext, global_config: dict):
    return await _process_bet_on_slim(client, event, user_ctx, global_config)


async def cleanup_message(client, message_ref):
    """安全地删除指定消息对象。"""
    if not message_ref:
        return
    try:
        await message_ref.delete()
        return
    except Exception:
        pass
    try:
        chat_id = getattr(message_ref, "chat_id", None)
        msg_id = getattr(message_ref, "id", None)
        if chat_id is not None and msg_id is not None:
            await client.delete_messages(chat_id, msg_id)
    except Exception:
        pass


async def process_red_packet(client, event, user_ctx: UserContext, global_config: dict):
    """处理红包消息，尝试领取。"""
    sender_id = getattr(event, "sender_id", None)
    zq_bot = user_ctx.config.groups.get("zq_bot")
    zq_bot_targets = {str(item) for item in _iter_targets(zq_bot)}
    if zq_bot_targets and str(sender_id) not in zq_bot_targets:
        return

    text = (getattr(event, "raw_text", None) or getattr(event, "text", None) or "").strip()
    if not text:
        return

    reply_markup = getattr(event, "reply_markup", None)
    rows = getattr(reply_markup, "rows", None) if reply_markup else None
    if not rows:
        return

    red_keywords = ("红包", "领取", "抢红包", "red", "packet", "hongbao", "claim")
    game_keywords = ("游戏", "对战", "闯关", "开局", "竞猜", "匹配", "挑战", "start game")
    lower_text = text.lower()

    callback_buttons = []
    red_button_candidates = []
    for row_idx, row in enumerate(rows):
        for btn_idx, btn in enumerate(getattr(row, "buttons", None) or []):
            btn_data = getattr(btn, "data", None)
            if not btn_data:
                continue
            btn_text = str(getattr(btn, "text", "") or "")
            try:
                data_text = btn_data.decode("utf-8", errors="ignore") if isinstance(btn_data, (bytes, bytearray)) else str(btn_data)
            except Exception:
                data_text = str(btn_data)

            text_l = btn_text.lower()
            data_l = data_text.lower()
            callback_buttons.append((row_idx, btn_idx, btn_data, text_l, data_l))

            if any(k in text_l for k in red_keywords) or any(k in data_l for k in red_keywords):
                red_button_candidates.append((row_idx, btn_idx, btn_data, text_l, data_l))

    if not callback_buttons:
        return

    has_red_text = ("灵石" in text and "红包" in text) or any(k in lower_text for k in ("抢红包", "领取红包"))
    has_game_hint = any(k in lower_text for k in game_keywords)

    # 仅处理明确红包消息；若是游戏提示且没有红包信号，直接忽略
    if not has_red_text and not red_button_candidates:
        return
    if has_game_hint and not has_red_text and not red_button_candidates:
        return

    # 优先红包候选按钮，否则回退第一个可点击按钮（兼容旧脚本）
    target_row_idx, target_btn_idx, button_data, _, _ = (
        red_button_candidates[0] if red_button_candidates else callback_buttons[0]
    )

    log_event(
        logging.INFO,
        "red_packet",
        "检测到红包按钮消息",
        user_id=user_ctx.user_id,
        msg_id=getattr(event, "id", None),
    )

    from telethon.tl import functions as tl_functions
    import re

    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            try:
                await event.click(target_row_idx, target_btn_idx)
            except Exception:
                await event.click(button_data)
            await asyncio.sleep(1)

            response = await client(
                tl_functions.messages.GetBotCallbackAnswerRequest(
                    peer=event.chat_id,
                    msg_id=event.id,
                    data=button_data,
                )
            )
            response_msg = getattr(response, "message", "") or ""

            if "已获得" in response_msg:
                bonus_match = re.search(r"已获得\s*(\d+)\s*灵石", response_msg)
                bonus = bonus_match.group(1) if bonus_match else "未知数量"
                mes = f"🎉 抢到红包{bonus}灵石！"
                log_event(
                    logging.INFO,
                    "red_packet",
                    "领取成功",
                    user_id=user_ctx.user_id,
                    bonus=bonus,
                )
                await send_to_admin(client, mes, user_ctx, global_config)
                return

            if any(flag in response_msg for flag in ("不能重复领取", "来晚了", "领过")):
                mes = "🧧 红包领取失败 🧧\n\n原因：来晚了，红包已被领完"
                log_event(
                    logging.INFO,
                    "red_packet",
                    "红包已领取或过期",
                    user_id=user_ctx.user_id,
                    response=response_msg,
                )
                await send_to_admin(client, mes, user_ctx, global_config)
                return

            log_event(
                logging.WARNING,
                "red_packet",
                "红包领取回复未知，准备重试",
                user_id=user_ctx.user_id,
                attempt=attempt + 1,
                response=response_msg[:80],
            )
        except Exception as e:
            log_event(
                logging.WARNING,
                "red_packet",
                "尝试领取红包失败",
                user_id=user_ctx.user_id,
                attempt=attempt + 1,
                error=str(e),
            )

        if attempt < max_attempts - 1:
            await asyncio.sleep(1)

    log_event(
        logging.WARNING,
        "red_packet",
        "多次尝试后未成功领取红包",
        user_id=user_ctx.user_id,
        msg_id=getattr(event, "id", None),
    )


def is_fund_available(user_ctx: UserContext, bet_amount: int = 0) -> bool:
    """检查资金是否充足（与 master 版语义一致：需同时满足余额>0且>=本次下注金额）。"""
    rt = user_ctx.state.runtime
    gambling_fund = rt.get("gambling_fund", 0)
    return gambling_fund > 0 and gambling_fund >= bet_amount


def _is_invalid_callback_message_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "message id is invalid",
        "getbotcallbackanswerrequest",
        "can't do that operation on such message",
        "messageidinvaliderror",
    )
    return any(marker in text for marker in markers)


async def _find_latest_bet_prompt_message(client, event, user_ctx: UserContext):
    """回溯最近可点击的下注提示消息，用于 message id 失效时恢复。"""
    zq_bot = user_ctx.config.groups.get("zq_bot")
    zq_bot_targets = {str(item) for item in _iter_targets(zq_bot)}
    hints = ("[近 40 次结果]", "由近及远", "0 小 1 大")

    try:
        async for msg in client.iter_messages(event.chat_id, limit=20):
            if zq_bot_targets and str(getattr(msg, "sender_id", None)) not in zq_bot_targets:
                continue
            if not getattr(msg, "reply_markup", None):
                continue
            raw = (getattr(msg, "message", None) or getattr(msg, "raw_text", None) or "").strip()
            if any(hint in raw for hint in hints):
                return msg
    except Exception as e:
        log_event(logging.DEBUG, "bet_on", "回溯下注提示消息失败", user_id=user_ctx.user_id, error=str(e))
    return None


async def _click_bet_button_with_recover(client, event, user_ctx: UserContext, button_data):
    """点击下注按钮；若原消息失效，则回溯最新下注提示消息重试。"""
    try:
        await event.click(button_data)
        return
    except Exception as e:
        if not _is_invalid_callback_message_error(e):
            raise

    latest_msg = await _find_latest_bet_prompt_message(client, event, user_ctx)
    if latest_msg is None:
        raise RuntimeError("下注窗口失效且未找到可用的最新下注消息")

    await latest_msg.click(button_data)
    log_event(
        logging.WARNING,
        "bet_on",
        "原下注消息失效，已使用最新消息重试按钮点击",
        user_id=user_ctx.user_id,
        src_msg=getattr(event, "id", None),
        retry_msg=getattr(latest_msg, "id", None),
    )


def _read_timing_config(global_config: dict) -> dict:
    """读取下注时序参数，提供安全兜底。"""
    cfg = global_config.get("betting") if isinstance(global_config.get("betting"), dict) else {}

    def _to_float(name: str, default: float, minimum: float, maximum: float) -> float:
        raw = cfg.get(name, default)
        try:
            val = float(raw)
        except Exception:
            return default
        return max(minimum, min(maximum, val))

    return {
        "prompt_wait_sec": _to_float("prompt_wait_sec", 1.2, 0.0, 5.0),
        "predict_timeout_sec": _to_float("predict_timeout_sec", 8.0, 1.0, 30.0),
        "click_interval_sec": _to_float("click_interval_sec", 0.45, 0.05, 2.0),
        "click_timeout_sec": _to_float("click_timeout_sec", 6.0, 1.0, 20.0),
    }


def calculate_bet_amount(rt: dict) -> int:
    """按 master 逻辑计算本局下注金额。"""
    win_count = rt.get("win_count", 0)
    lose_count = rt.get("lose_count", 0)
    initial_amount = int(rt.get("initial_amount", 500))
    lose_stop = int(rt.get("lose_stop", 13))
    lose_once = float(rt.get("lose_once", 3))
    lose_twice = float(rt.get("lose_twice", 2.1))
    lose_three = float(rt.get("lose_three", 2.1))
    lose_four = float(rt.get("lose_four", 2.05))

    if win_count >= 0 and lose_count == 0:
        return constants.closest_multiple_of_500(initial_amount)

    if (lose_count + 1) > lose_stop:
        return 0

    base_amount = int(rt.get("bet_amount", initial_amount))
    if lose_count == 1:
        target = base_amount * lose_once
    elif lose_count == 2:
        target = base_amount * lose_twice
    elif lose_count == 3:
        target = base_amount * lose_three
    else:
        target = base_amount * lose_four

    # 与 master 一致：补 1% 安全边际
    return constants.closest_multiple_of_500(target + target * 0.01)


def _build_pause_resume_hint(rt: dict) -> str:
    """构建“暂停结束后会做什么”的提示。"""
    next_sequence = int(rt.get("bet_sequence_count", 0)) + 1
    next_amount = int(calculate_bet_amount(rt) or 0)
    if next_amount > 0:
        return f"恢复后动作：继续第 {next_sequence} 手，预计下注 {format_number(next_amount)}"
    return f"恢复后动作：继续第 {next_sequence} 手"


def _format_predict_signal_brief(rt: dict) -> str:
    """把模型信号整理成易读短句，用于暂停恢复提示。"""
    source = str(rt.get("last_predict_source", "unknown") or "unknown")
    tag = str(rt.get("last_predict_tag", "") or "UNKNOWN")
    confidence = int(rt.get("last_predict_confidence", 0) or 0)
    reason = str(rt.get("last_predict_reason", "") or "").strip()
    if reason:
        return f"来源 {source} | 标签 {tag} | 置信度 {confidence}% | 理由 {reason}"
    return f"来源 {source} | 标签 {tag} | 置信度 {confidence}%"


def _get_history_tail_streak(history: list) -> tuple:
    """返回历史尾部连庄信息：(连庄长度, 连庄方向0/1)。"""
    if not isinstance(history, list) or not history:
        return 0, -1
    try:
        tail_value = int(history[-1])
    except Exception:
        return 0, -1
    streak = 1
    for idx in range(len(history) - 2, -1, -1):
        try:
            current = int(history[idx])
        except Exception:
            break
        if current != tail_value:
            break
        streak += 1
    return streak, tail_value


def _should_skip_repeated_entry_timeout_gate(rt: dict, next_sequence: int, settled_count: int) -> bool:
    """
    防止同一连押阶段在“无新结算”的情况下，因模型持续超时而反复触发暂停。
    仅用于“模型可用性门控（超时）”去重。
    """
    last_seq_raw = rt.get("entry_timeout_gate_last_seq", -1)
    last_settled_raw = rt.get("entry_timeout_gate_last_settled", -1)
    try:
        last_seq = int(last_seq_raw)
    except Exception:
        last_seq = -1
    try:
        last_settled = int(last_settled_raw)
    except Exception:
        last_settled = -1
    if last_seq == int(next_sequence) and last_settled == int(settled_count):
        return True
    rt["entry_timeout_gate_last_seq"] = int(next_sequence)
    rt["entry_timeout_gate_last_settled"] = int(settled_count)
    return False


def _clear_hand_stall_guard(rt: dict) -> None:
    """清理“同手位卡死防护”计数器。"""
    rt["stall_guard_sequence"] = -1
    rt["stall_guard_last_history_len"] = -1
    rt["stall_guard_no_bet_streak"] = 0
    rt["stall_guard_skip_streak"] = 0
    rt["stall_guard_timeout_streak"] = 0
    rt["stall_guard_gate_streak"] = 0


def _record_hand_stall_block(rt: dict, next_sequence: int, history_len: int, reason: str) -> dict:
    """
    记录同手位“未下单”阻断事件，并判断是否触发防卡死解锁。
    reason: skip/timeout/gate
    """
    reason = str(reason or "gate").strip().lower()
    if reason not in {"skip", "timeout", "gate"}:
        reason = "gate"

    current_seq = int(rt.get("stall_guard_sequence", -1))
    if current_seq != int(next_sequence):
        _clear_hand_stall_guard(rt)
        rt["stall_guard_sequence"] = int(next_sequence)

    last_history_len = int(rt.get("stall_guard_last_history_len", -1))
    if int(history_len) != last_history_len:
        rt["stall_guard_last_history_len"] = int(history_len)
        rt["stall_guard_no_bet_streak"] = int(rt.get("stall_guard_no_bet_streak", 0)) + 1
        if reason == "skip":
            rt["stall_guard_skip_streak"] = int(rt.get("stall_guard_skip_streak", 0)) + 1
        elif reason == "timeout":
            rt["stall_guard_timeout_streak"] = int(rt.get("stall_guard_timeout_streak", 0)) + 1
        else:
            rt["stall_guard_gate_streak"] = int(rt.get("stall_guard_gate_streak", 0)) + 1

    no_bet_streak = int(rt.get("stall_guard_no_bet_streak", 0))
    skip_streak = int(rt.get("stall_guard_skip_streak", 0))
    timeout_streak = int(rt.get("stall_guard_timeout_streak", 0))
    gate_streak = int(rt.get("stall_guard_gate_streak", 0))

    force_unlock = (
        skip_streak > STALL_GUARD_SKIP_MAX
        or timeout_streak > STALL_GUARD_TIMEOUT_MAX
        or no_bet_streak > STALL_GUARD_TOTAL_MAX
    )
    return {
        "force_unlock": force_unlock,
        "sequence": int(next_sequence),
        "reason": reason,
        "no_bet_streak": no_bet_streak,
        "skip_streak": skip_streak,
        "timeout_streak": timeout_streak,
        "gate_streak": gate_streak,
    }


def _prepare_force_unlock_prediction(state, rt: dict, next_sequence: int, trigger: dict) -> int:
    """生成防卡死强制解锁预测方向（统计兜底）。"""
    prediction = int(fallback_prediction(state.history))
    rt["last_predict_source"] = "unlock_fallback"
    rt["last_predict_tag"] = "UNLOCK"
    rt["last_predict_confidence"] = 0
    rt["last_predict_reason"] = "同手位连续阻断，强制解锁"
    rt["last_predict_info"] = (
        "防卡死解锁 | "
        f"第{next_sequence}手连续阻断 "
        f"(总:{trigger.get('no_bet_streak', 0)}, "
        f"skip:{trigger.get('skip_streak', 0)}, timeout:{trigger.get('timeout_streak', 0)}) "
        f"| 兜底方向:{'大' if prediction == 1 else '小'}"
    )
    rt["stall_guard_force_unlock_total"] = int(rt.get("stall_guard_force_unlock_total", 0)) + 1
    return prediction


def _select_secondary_model_id(user_ctx: UserContext, primary_model_id: str) -> str:
    """
    从模型链中选择“不同于主模型”的副模型，用于高阶手位二次确认。
    若不存在可用副模型，返回空字符串。
    """
    try:
        model_mgr = user_ctx.get_model_manager()
        primary_cfg = model_mgr.get_model(str(primary_model_id))
        primary_actual = str(primary_cfg.get("model_id")) if primary_cfg else str(primary_model_id)

        ordered_models = []
        chain = list(model_mgr.fallback_chain or [])
        if chain:
            for key in chain:
                cfg = model_mgr.get_model(str(key))
                if not cfg or not cfg.get("enabled", True):
                    continue
                mid = str(cfg.get("model_id", "")).strip()
                if mid and mid not in ordered_models:
                    ordered_models.append(mid)
        else:
            for cfg in model_mgr.models:
                if not cfg.get("enabled", True):
                    continue
                mid = str(cfg.get("model_id", "")).strip()
                if mid and mid not in ordered_models:
                    ordered_models.append(mid)

        if not ordered_models:
            return ""

        if primary_actual in ordered_models:
            idx = ordered_models.index(primary_actual)
            for mid in ordered_models[idx + 1:]:
                if mid != primary_actual:
                    return mid
            for mid in ordered_models[:idx]:
                if mid != primary_actual:
                    return mid
            return ""

        for mid in ordered_models:
            if mid != primary_actual:
                return mid
    except Exception:
        return ""
    return ""


def _is_neutral_long_term_gap(value: float) -> bool:
    try:
        current = float(value)
    except (TypeError, ValueError):
        return False
    return NEUTRAL_LONG_TERM_GAP_LOW <= current <= NEUTRAL_LONG_TERM_GAP_HIGH


def _evaluate_high_pressure_pattern_gate(rt: dict, risk_pause: dict, next_sequence: int) -> dict:
    """
    深度风控开启时的高压位结构门控：
    - 第3手起，不稳定形态需要更高置信度
    - 第5手起，不稳定形态/候选长龙默认从严，优先 SKIP / 暂停
    """
    if next_sequence < 3:
        return {"blocked": False}

    source = str(rt.get("last_predict_source", "unknown")).lower().strip()
    if source != "model":
        return {"blocked": False}

    tag = str(rt.get("last_predict_tag", "") or "UNKNOWN").strip().upper()
    confidence = int(rt.get("last_predict_confidence", 0) or 0)
    tail_len = int(rt.get("last_predict_tail_len", 0) or 0)
    long_term_gap = float(rt.get("last_predict_long_term_gap", 0.5) or 0.5)
    wins = int(risk_pause.get("wins", 0))
    total = int(risk_pause.get("total", 0))
    win_rate = (wins / total) if total > 0 else 0.0

    reasons = []
    pause_rounds = 1 if next_sequence < HIGH_PRESSURE_SKIP_MIN_STEP else HIGH_PRESSURE_PATTERN_PAUSE_ROUNDS
    gate_name = "高压位结构门控"

    if tag in UNSTABLE_PATTERN_TAGS:
        conf_threshold = UNSTABLE_PATTERN_MIN_CONF_STEP3 if next_sequence < HIGH_PRESSURE_SKIP_MIN_STEP else UNSTABLE_PATTERN_MIN_CONF_STEP5
        if confidence < conf_threshold:
            reasons.append(f"不稳定形态 {tag} 置信度仅 {confidence}% < {conf_threshold}%")
        if tail_len < 4:
            reasons.append(f"尾部连数仅 {tail_len}，形态未成熟")
        if _is_neutral_long_term_gap(long_term_gap):
            reasons.append(f"长期100局占比 {long_term_gap:.2f} 接近均衡，不能作为下注证据")
        if next_sequence >= HIGH_PRESSURE_SKIP_MIN_STEP:
            reasons.append(f"第{HIGH_PRESSURE_SKIP_MIN_STEP}手及以上不接受 {tag} 直接下注")
    elif tag == "DRAGON_CANDIDATE" and next_sequence >= HIGH_PRESSURE_SKIP_MIN_STEP:
        if tail_len < DRAGON_CANDIDATE_MIN_TAIL_STEP5:
            reasons.append(f"DRAGON_CANDIDATE 尾部连数仅 {tail_len} < {DRAGON_CANDIDATE_MIN_TAIL_STEP5}")
        if confidence < HIGH_PRESSURE_SKIP_MIN_CONF:
            reasons.append(f"候选长龙置信度仅 {confidence}% < {HIGH_PRESSURE_SKIP_MIN_CONF}%")
        if _is_neutral_long_term_gap(long_term_gap):
            reasons.append(f"长期100局占比 {long_term_gap:.2f} 接近均衡，长龙证据不足")
    elif tag == "LONG_DRAGON" and next_sequence >= HIGH_PRESSURE_SKIP_MIN_STEP:
        if tail_len < DRAGON_CANDIDATE_MIN_TAIL_STEP5:
            reasons.append(f"LONG_DRAGON 尾部连数仅 {tail_len}，成熟度不足")
        if confidence < HIGH_PRESSURE_SKIP_MIN_CONF:
            reasons.append(f"LONG_DRAGON 置信度仅 {confidence}% < {HIGH_PRESSURE_SKIP_MIN_CONF}%")

    if reasons:
        return {
            "blocked": True,
            "gate_name": gate_name,
            "pause_rounds": pause_rounds,
            "reason_text": "；".join(reasons),
            "source": source,
            "tag": tag,
            "confidence": confidence,
            "wins": wins,
            "total": total,
            "win_rate": win_rate,
        }
    return {"blocked": False}


async def _evaluate_high_step_double_confirm(
    user_ctx: UserContext,
    risk_pause: dict,
    next_sequence: int,
    primary_prediction: int,
    primary_confidence: int,
) -> dict:
    """
    第5手起执行二次确认：主模型 + 副模型必须同向且置信度达标。
    """
    if next_sequence < HIGH_STEP_DOUBLE_CONFIRM_MIN_STEP:
        return {"blocked": False}

    reasons = []
    gate_name = f"第{HIGH_STEP_DOUBLE_CONFIRM_MIN_STEP}手双模型确认门控"
    pause_rounds = HIGH_STEP_DOUBLE_CONFIRM_PAUSE_ROUNDS
    wins = int(risk_pause.get("wins", 0))
    total = int(risk_pause.get("total", 0))
    win_rate = (wins / total) if total > 0 else 0.0
    primary_model_id = str(user_ctx.state.runtime.get("current_model_id", ""))

    if primary_prediction not in (0, 1):
        reasons.append("主模型未给出可下注方向")
    if int(primary_confidence or 0) < HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF:
        reasons.append(
            f"主模型置信度 {int(primary_confidence or 0)}% < {HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF}%"
        )

    secondary_model_id = _select_secondary_model_id(user_ctx, primary_model_id)
    secondary_confidence = 0
    secondary_prediction = -1
    secondary_source = "none"

    if secondary_model_id:
        history = user_ctx.state.history
        short_term_20 = history[-20:] if len(history) >= 20 else history[:]
        medium_term_50 = history[-50:] if len(history) >= 50 else history[:]
        short_str = "".join("1" if x == 1 else "0" for x in short_term_20)
        medium_str = "".join("1" if x == 1 else "0" for x in medium_term_50)
        pattern = extract_pattern_features(history)
        trend_gap = calculate_trend_gap(history, window=100)
        tail_streak_len = int(pattern.get("tail_streak_len", 0) or 0)
        tail_side = "大" if int(pattern.get("tail_streak_char", 0) or 0) == 1 else "小"
        gap = int(trend_gap.get("gap", 0) or 0)
        main_dir = "大" if primary_prediction == 1 else "小"

        prompt = f"""你是风控复核模型，只输出JSON。
当前处于倍投第{next_sequence}手（高风险手位），请做方向复核：
- 主模型方向：{main_dir}
- 主模型置信度：{int(primary_confidence or 0)}%
- 最近20局：{short_str}
- 最近50局：{medium_str}
- 尾部形态：{pattern.get('pattern_tag', 'UNKNOWN')}（{tail_streak_len}连{tail_side}）
- 缺口：{gap:+d}

只输出JSON：
{{"prediction": -1或0或1, "confidence": 1-100, "reason": "20字内"}}"""

        messages = [
            {"role": "system", "content": "你是高风险入场复核器，只返回JSON。"},
            {"role": "user", "content": prompt},
        ]

        try:
            result = await asyncio.wait_for(
                user_ctx.get_model_manager().call_model(
                    secondary_model_id,
                    messages,
                    temperature=0.0,
                    max_tokens=120,
                ),
                timeout=HIGH_STEP_DOUBLE_CONFIRM_MODEL_TIMEOUT_SEC,
            )
            if not result.get("success"):
                raise RuntimeError(str(result.get("error", "unknown")))
            parsed = parse_analysis_result_insight(
                result.get("content", ""),
                default_prediction=primary_prediction,
            )
            secondary_prediction = int(parsed.get("prediction", -1))
            secondary_confidence = int(parsed.get("confidence", 0) or 0)
            secondary_source = secondary_model_id

            if secondary_prediction != primary_prediction:
                if secondary_prediction == -1:
                    reasons.append("副模型建议观望（SKIP）")
                else:
                    side = "大" if secondary_prediction == 1 else "小"
                    reasons.append(f"副模型方向不一致（副模型={side}）")
            if secondary_confidence < HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF:
                reasons.append(
                    f"副模型置信度 {secondary_confidence}% < {HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF}%"
                )
        except Exception as e:
            secondary_source = "error"
            reasons.append(f"副模型复核失败：{str(e)[:60]}")
    else:
        # 无副模型时，启用更严格单模型兜底，避免高风险手位盲目继续。
        if int(primary_confidence or 0) < (HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF + 5):
            reasons.append(
                f"无副模型时主模型置信度需 >= {HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF + 5}%"
            )
        secondary_source = "single"

    if reasons:
        return {
            "blocked": True,
            "gate_name": gate_name,
            "pause_rounds": pause_rounds,
            "reason_text": "；".join(reasons),
            "source": f"primary={primary_model_id},secondary={secondary_source}",
            "tag": str(user_ctx.state.runtime.get("last_predict_tag", "") or "UNKNOWN"),
            "confidence": int(primary_confidence or 0),
            "wins": wins,
            "total": total,
            "win_rate": win_rate,
        }
    return {"blocked": False}


def _clear_shadow_probe(rt: dict) -> None:
    rt["shadow_probe_active"] = False
    rt["shadow_probe_origin_reason"] = ""
    rt["shadow_probe_target_rounds"] = 0
    rt["shadow_probe_pass_required"] = 0
    rt["shadow_probe_checked"] = 0
    rt["shadow_probe_hits"] = 0
    rt["shadow_probe_pending_prediction"] = None
    rt["shadow_probe_last_history_len"] = -1


def _start_shadow_probe(rt: dict, reason: str) -> None:
    _clear_shadow_probe(rt)
    rt["shadow_probe_active"] = True
    rt["shadow_probe_origin_reason"] = str(reason or "风控暂停").strip() or "风控暂停"
    rt["shadow_probe_target_rounds"] = int(SHADOW_PROBE_ROUNDS)
    rt["shadow_probe_pass_required"] = int(SHADOW_PROBE_PASS_REQUIRED)
    rt["shadow_probe_checked"] = 0
    rt["shadow_probe_hits"] = 0
    rt["shadow_probe_pending_prediction"] = None
    rt["shadow_probe_last_history_len"] = -1


def _should_start_shadow_after_pause(rt: dict) -> bool:
    if not SHADOW_PROBE_ENABLED:
        return False
    if rt.get("manual_pause", False):
        return False
    reason = str(rt.get("pause_countdown_reason", "")).strip()
    if not reason:
        return False
    return any(token in reason for token in ("风控", "高倍入场", "模型可用性门控"))


def _consume_shadow_probe_settle_result(rt: dict, result: int) -> dict:
    """
    在结算阶段消费影子验证的待评估预测，并推进影子验证状态机。
    返回结构:
    {
      "updated": bool,
      "hit": bool,
      "checked": int,
      "hits": int,
      "target_rounds": int,
      "pass_required": int,
      "done": bool,
      "passed": bool,
      "pause_rounds": int,
    }
    """
    if not rt.get("shadow_probe_active", False):
        return {"updated": False}

    pending_pred = rt.get("shadow_probe_pending_prediction", None)
    if pending_pred not in (0, 1):
        return {"updated": False}

    target_rounds = max(1, int(rt.get("shadow_probe_target_rounds", SHADOW_PROBE_ROUNDS)))
    pass_required = max(1, int(rt.get("shadow_probe_pass_required", SHADOW_PROBE_PASS_REQUIRED)))
    checked = int(rt.get("shadow_probe_checked", 0))
    hits = int(rt.get("shadow_probe_hits", 0))

    checked += 1
    hit = int(pending_pred) == int(result)
    if hit:
        hits += 1

    rt["shadow_probe_pending_prediction"] = None
    rt["shadow_probe_checked"] = checked
    rt["shadow_probe_hits"] = hits

    done = checked >= target_rounds
    passed = False
    pause_rounds = 0

    if done:
        if hits >= pass_required:
            passed = True
            _clear_shadow_probe(rt)
            rt["bet_on"] = True
            rt["mode_stop"] = True
            rt["pause_resume_pending"] = True
            rt["pause_resume_pending_reason"] = "影子验证通过"
            rt["pause_resume_probe_settled"] = -1
        else:
            _clear_shadow_probe(rt)
            rt["shadow_probe_rearm"] = True
            pause_rounds = int(SHADOW_PROBE_RETRY_PAUSE_ROUNDS)
            _enter_pause(rt, pause_rounds, "影子验证未达标")

    return {
        "updated": True,
        "hit": bool(hit),
        "checked": checked,
        "hits": hits,
        "target_rounds": target_rounds,
        "pass_required": pass_required,
        "done": done,
        "passed": passed,
        "pause_rounds": pause_rounds,
    }


def _evaluate_entry_quality_gate(rt: dict, risk_pause: dict, next_sequence: int) -> dict:
    """
    高倍入场质量门控：
    - 第3手：至少满足最低置信度，避免在弱信号下继续放大
    - 第4手：更严格，且限制标签白名单
    """
    if next_sequence not in (3, 4):
        return {"blocked": False}

    source = str(rt.get("last_predict_source", "unknown")).lower()
    tag = str(rt.get("last_predict_tag", "")).strip().upper()
    confidence = int(rt.get("last_predict_confidence", 0) or 0)
    total = int(risk_pause.get("total", 0))
    wins = int(risk_pause.get("wins", 0))
    win_rate = (wins / total) if total > 0 else 0.0

    reasons = []
    pause_rounds = ENTRY_GUARD_STEP3_PAUSE_ROUNDS
    gate_name = "第3手质量门控"

    if source != "model":
        reasons.append("本局预测未拿到稳定模型结果（超时/异常）")

    if next_sequence == 3:
        if confidence < ENTRY_GUARD_STEP3_MIN_CONF:
            reasons.append(f"置信度 {confidence}% < {ENTRY_GUARD_STEP3_MIN_CONF}%")
    elif next_sequence == 4:
        gate_name = "第4手强风控门控"
        pause_rounds = ENTRY_GUARD_STEP4_PAUSE_ROUNDS
        # 样本不足阶段（<40笔）放宽：仅检查置信度，避免第4手过早频繁拦截。
        step4_conf_threshold = ENTRY_GUARD_STEP4_MIN_CONF if total >= RISK_WINDOW_BETS else ENTRY_GUARD_STEP4_MIN_CONF_EARLY
        if confidence < step4_conf_threshold:
            reasons.append(f"置信度 {confidence}% < {step4_conf_threshold}%")
        # 白名单与胜率检查仅在样本充足后生效。
        if total >= RISK_WINDOW_BETS:
            if tag not in ENTRY_GUARD_STEP4_ALLOWED_TAGS:
                reasons.append(f"标签 {tag or 'UNKNOWN'} 不在白名单")
            if win_rate < 0.45:
                reasons.append(f"最近40笔胜率仅 {wins}/{total}（{win_rate * 100:.1f}%）")

    if reasons:
        return {
            "blocked": True,
            "gate_name": gate_name,
            "pause_rounds": pause_rounds,
            "reason_text": "；".join(reasons),
            "source": source,
            "tag": tag or "UNKNOWN",
            "confidence": confidence,
            "wins": wins,
            "total": total,
            "win_rate": win_rate,
        }
    return {"blocked": False}


async def _apply_entry_gate_pause(
    client,
    user_ctx: UserContext,
    global_config: dict,
    gate: dict,
    next_sequence: int,
) -> None:
    """统一发送高倍入场门控暂停提示。"""
    rt = user_ctx.state.runtime
    pause_rounds = max(1, int(gate.get("pause_rounds", 1)))
    _enter_pause(rt, pause_rounds, gate.get("gate_name", "高倍入场门控"))
    user_ctx.save_state()

    total = int(gate.get("total", 0) or 0)
    wins = int(gate.get("wins", 0) or 0)
    if total > 0:
        wr_text = f"{wins}/{total}（{gate.get('win_rate', 0.0) * 100:.1f}%）"
    else:
        wr_text = "样本不足（N/A）"

    pause_msg = (
        f"⛔ 自动风控暂停 ⛔\n\n"
        f"触发点：第 {next_sequence} 手下注前\n"
        f"触发类型：{gate.get('gate_name', '高倍入场门控')}\n"
        f"当前信号：标签 {gate.get('tag', 'UNKNOWN')} | 置信度 {gate.get('confidence', 0)}% | 来源 {gate.get('source', 'unknown')}\n"
        f"最近胜率：{wr_text}\n"
        f"未通过条件：{gate.get('reason_text', '信号质量不足')}\n"
        f"本次暂停：{pause_rounds} 局\n"
        f"暂停期间：保留当前倍投进度，不会重置首注\n"
        f"{_build_pause_resume_hint(rt)}"
    )

    if hasattr(user_ctx, "risk_pause_message") and user_ctx.risk_pause_message:
        await cleanup_message(client, user_ctx.risk_pause_message)
    user_ctx.risk_pause_message = await send_to_admin(client, pause_msg, user_ctx, global_config)
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=pause_rounds,
    )

def _get_recent_settled_outcomes(state, window: int = RISK_WINDOW_BETS) -> list:
    """提取最近 N 笔已结算结果（赢=1，输=0）。"""
    if window <= 0:
        return []
    outcomes = []
    for entry in reversed(_get_strategy_bet_sequence_log(state)):
        result = entry.get("result")
        if result == "赢":
            outcomes.append(1)
        elif result == "输":
            outcomes.append(0)
        if len(outcomes) >= window:
            break
    outcomes.reverse()
    return outcomes


def _count_settled_bets(state) -> int:
    """统计已结算押注笔数（赢/输）。"""
    count = 0
    for entry in _get_strategy_bet_sequence_log(state):
        result = entry.get("result")
        if result in ("赢", "输"):
            count += 1
    return count


def _fallback_pause_rounds(level: str, wins: int, total: int, lose_count: int, max_pause: int) -> int:
    """模型不可用时的暂停局数兜底。"""
    max_pause = max(1, int(max_pause))
    if total <= 0:
        return min(1, max_pause)

    win_rate = wins / total
    if str(level).startswith("DEEP"):
        if lose_count >= 9:
            base = 2
        elif lose_count >= 6:
            base = 2
        else:
            base = 3
        return max(1, min(max_pause, base))

    # BASE：根据40局胜率分层
    if win_rate <= 0.30:
        base = 4
    elif win_rate <= 0.35:
        base = 3
    else:
        base = 2
    return max(1, min(max_pause, base))


def _parse_pause_rounds_response(raw_text: str, max_pause: int) -> tuple:
    """解析模型返回的暂停建议，返回 (pause_rounds|None, reason)。"""
    if not raw_text:
        return None, ""

    max_pause = max(1, int(max_pause))
    candidates = [raw_text.strip()]
    # 兼容模型返回前后包裹说明文字的情况
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw_text[start:end + 1].strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if not isinstance(data, dict):
                continue
            pause_raw = data.get("pause_rounds", data.get("pause", data.get("rounds")))
            if pause_raw is None:
                continue
            pause_rounds = int(float(str(pause_raw).strip()))
            pause_rounds = max(1, min(max_pause, pause_rounds))
            reason = str(data.get("reason", "")).strip()
            return pause_rounds, reason
        except Exception:
            continue

    return None, ""


async def _suggest_pause_rounds_by_model(
    user_ctx: UserContext,
    risk_eval: dict,
    max_pause: int,
) -> tuple:
    """调用大模型给出暂停局数建议，失败时自动降级到统计兜底。"""
    state = user_ctx.state
    rt = state.runtime
    current_model_id = rt.get("current_model_id")
    wins = int(risk_eval.get("wins", 0))
    total = int(risk_eval.get("total", 0))
    lose_count = int(risk_eval.get("lose_count", 0))
    level = str(risk_eval.get("level", "BASE"))

    fallback_rounds = _fallback_pause_rounds(level, wins, total, lose_count, max_pause)
    fallback_reason = "模型异常，统计兜底"
    if not current_model_id:
        return fallback_rounds, fallback_reason, "fallback"

    recent_tail = risk_eval.get("recent_outcomes", [])[-12:]
    recent_text = "".join(str(x) for x in recent_tail) if recent_tail else "NA"
    prompt = f"""你是一个只负责风险暂停局数的控制器。必须只输出JSON。

当前风控层级：{risk_eval.get('level_label', level)}
最近{total}笔胜率：{wins}/{total}（{risk_eval.get('win_rate', 0.0) * 100:.1f}%）
当前连输：{lose_count}
下一手：第{risk_eval.get('next_sequence', 1)}手
最近12笔结算(赢1输0)：{recent_text}

请给出暂停建议，范围必须在 1 到 {max_pause} 之间。
输出格式：
{{"pause_rounds": 1-{max_pause}之间整数, "reason": "20字内"}}
"""

    messages = [
        {"role": "system", "content": "你是交易风控引擎，只返回JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]

    try:
        result = await asyncio.wait_for(
            user_ctx.get_model_manager().call_model(current_model_id, messages, temperature=0.0, max_tokens=120),
            timeout=RISK_PAUSE_MODEL_TIMEOUT_SEC,
        )
        if not result.get("success"):
            raise RuntimeError(str(result.get("error", "unknown")))

        rounds, reason = _parse_pause_rounds_response(result.get("content", ""), max_pause=max_pause)
        if rounds is None:
            raise ValueError("pause_rounds parse failed")
        reason = reason or "模型建议"
        return rounds, reason, "model"
    except Exception as e:
        log_event(
            logging.WARNING,
            "risk_pause",
            "风控暂停模型建议失败，使用统计兜底",
            user_id=user_ctx.user_id,
            error=str(e),
            level=level,
        )
        return fallback_rounds, fallback_reason, "fallback"


def _get_deep_triggered_milestones(rt: dict) -> list:
    """读取并规范化已触发的深度风控里程碑。"""
    raw = rt.get("risk_deep_triggered_milestones", [])
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        items = []

    normalized = []
    for item in items:
        try:
            normalized.append(int(item))
        except Exception:
            continue
    return sorted(set(normalized))


def _evaluate_auto_risk_pause(state, rt: dict, next_sequence: int) -> dict:
    """
    评估自动风控状态（基础风控 + 深度风控里程碑）。
    基础风控：最近40笔胜率阈值触发（连续命中由外层控制）
    深度风控：连输每达到 3 的倍数档位时触发（每档同一连输周期仅触发一次）
    """
    outcomes = _get_recent_settled_outcomes(state, RISK_WINDOW_BETS)
    total = len(outcomes)
    wins = int(sum(outcomes))
    win_rate = wins / total if total > 0 else 0.0
    lose_count = int(rt.get("lose_count", 0))
    base_window_ready = total >= RISK_WINDOW_BETS
    base_trigger = base_window_ready and wins <= RISK_BASE_TRIGGER_WINS
    recovery_hit = base_window_ready and wins >= RISK_RECOVERY_WINS

    triggered_milestones = _get_deep_triggered_milestones(rt)
    deep_milestone = 0
    deep_level_cap = 0
    lose_stop = max(1, int(rt.get("lose_stop", 13)))
    if lose_count >= RISK_DEEP_TRIGGER_INTERVAL and lose_count < lose_stop:
        current_milestone = (lose_count // RISK_DEEP_TRIGGER_INTERVAL) * RISK_DEEP_TRIGGER_INTERVAL
        if current_milestone > 0 and current_milestone not in triggered_milestones:
            deep_milestone = current_milestone
            if current_milestone == RISK_DEEP_TRIGGER_INTERVAL:
                deep_level_cap = int(RISK_DEEP_FIRST_MAX_PAUSE_ROUNDS)
            else:
                deep_level_cap = int(RISK_DEEP_NEXT_MAX_PAUSE_ROUNDS)

    reasons = []
    if base_trigger:
        reasons.append("最近40笔胜率<=37.5%")
    if deep_milestone > 0:
        reasons.append(f"连输达到{deep_milestone}局档位（每3局触发）")

    return {
        "triggered": bool(base_trigger or deep_milestone > 0),
        "wins": wins,
        "total": total,
        "win_rate": win_rate,
        "next_sequence": next_sequence,
        "lose_count": lose_count,
        "base_window_ready": base_window_ready,
        "base_trigger": base_trigger,
        "recovery_hit": recovery_hit,
        "deep_trigger": deep_milestone > 0,
        "deep_milestone": deep_milestone,
        "deep_level_cap": deep_level_cap,
        "deep_triggered_milestones": triggered_milestones,
        "reasons": reasons,
        "recent_outcomes": outcomes[-20:],
    }


def _apply_auto_risk_pause(rt: dict, pause_rounds: int) -> None:
    """
    执行自动风控暂停。
    说明：stop_count 在下注入口每轮先减1，设为 (暂停局数+1) 才能真正停满指定局数。
    """
    pause_rounds = max(1, int(pause_rounds))
    internal_stop_count = pause_rounds + 1

    rt["stop_count"] = max(int(rt.get("stop_count", 0)), internal_stop_count)
    rt["bet_on"] = False
    rt["bet"] = False
    rt["mode_stop"] = False


def _enter_pause(rt: dict, pause_rounds: int, reason: str) -> int:
    """
    统一暂停入口：写入暂停状态 + 倒计时上下文。
    返回规范化后的暂停局数。
    """
    rounds = max(1, int(pause_rounds))
    _apply_auto_risk_pause(rt, rounds)
    _set_pause_countdown_context(rt, reason, rounds)
    return rounds


def _set_pause_countdown_context(rt: dict, reason: str, pause_rounds: int) -> None:
    """写入统一暂停倒计时上下文（手动暂停不使用该机制）。"""
    rounds = max(1, int(pause_rounds))
    rt["pause_countdown_active"] = True
    rt["pause_countdown_reason"] = str(reason or "自动暂停")
    rt["pause_countdown_total_rounds"] = rounds
    rt["pause_countdown_last_remaining"] = -1
    # 每次进入新暂停周期后，恢复复核提示应重新可发送一次。
    rt["pause_resume_probe_settled"] = -1


async def _clear_pause_countdown_notice(client, user_ctx: UserContext) -> None:
    """清理暂停倒计时消息与上下文。"""
    rt = user_ctx.state.runtime
    if hasattr(user_ctx, "pause_countdown_message") and user_ctx.pause_countdown_message:
        await cleanup_message(client, user_ctx.pause_countdown_message)
        user_ctx.pause_countdown_message = None
    rt["pause_countdown_active"] = False
    rt["pause_countdown_reason"] = ""
    rt["pause_countdown_total_rounds"] = 0
    rt["pause_countdown_last_remaining"] = -1


async def _refresh_pause_countdown_notice(
    client,
    user_ctx: UserContext,
    global_config: dict,
    remaining_rounds: int = None,
) -> None:
    """刷新式推送暂停倒计时通知。"""
    rt = user_ctx.state.runtime
    if rt.get("manual_pause", False):
        return
    if not rt.get("pause_countdown_active", False):
        return

    total_rounds = int(rt.get("pause_countdown_total_rounds", 0))
    if total_rounds <= 0:
        return

    if remaining_rounds is None:
        remaining_rounds = int(rt.get("stop_count", 0))
    remaining_rounds = max(0, min(total_rounds, int(remaining_rounds)))

    if remaining_rounds <= 0:
        return

    last_remaining = int(rt.get("pause_countdown_last_remaining", -1))
    if (
        last_remaining == remaining_rounds
        and hasattr(user_ctx, "pause_countdown_message")
        and user_ctx.pause_countdown_message
    ):
        return

    reason = str(rt.get("pause_countdown_reason", "自动暂停")).strip() or "自动暂停"
    progress_rounds = max(0, total_rounds - remaining_rounds)
    resume_hint = _build_pause_resume_hint(rt)
    countdown_msg = (
        "⏸️ 暂停倒计时提醒（自动） ⏸️\n\n"
        f"📌 暂停原因：{reason}\n"
        "🧱 当前状态：暂停中，本局不会下注\n"
        f"🔢 倒计时：{remaining_rounds} 局\n"
        f"📊 暂停进度：{progress_rounds}/{total_rounds}\n"
        f"🔄 {resume_hint}\n"
        "ℹ️ 若恢复时仍不满足风控门槛，会再次自动暂停"
    )

    if hasattr(user_ctx, "pause_countdown_message") and user_ctx.pause_countdown_message:
        await cleanup_message(client, user_ctx.pause_countdown_message)
    user_ctx.pause_countdown_message = await send_to_admin(client, countdown_msg, user_ctx, global_config)
    rt["pause_countdown_last_remaining"] = remaining_rounds


async def _trigger_deep_risk_pause_after_settle(
    client,
    user_ctx: UserContext,
    global_config: dict,
    risk_pause: dict,
    next_sequence: int,
    settled_count: int,
) -> bool:
    """在结算阶段触发深度风控暂停（连输里程碑），命中后立即通知。"""
    rt = user_ctx.state.runtime
    if not bool(rt.get("risk_deep_enabled", True)):
        return False
    if not risk_pause.get("deep_trigger", False):
        return False

    deep_milestone = int(risk_pause.get("deep_milestone", 0))
    deep_cap = int(risk_pause.get("deep_level_cap", 3))
    if deep_milestone <= 0 or deep_cap <= 0:
        return False

    # 长龙盘面放宽：避免“连续长龙 + 深度风控”叠加导致长时间停摆。
    original_deep_cap = deep_cap
    tail_len, tail_side = _get_history_tail_streak(user_ctx.state.history)
    deep_cap_adjust_reason = ""
    if tail_len >= RISK_DEEP_LONG_DRAGON_TAIL_LEN:
        deep_cap = max(1, min(deep_cap, int(RISK_DEEP_LONG_DRAGON_MAX_PAUSE_ROUNDS)))
        if deep_cap < original_deep_cap:
            side_text = "大" if tail_side == 1 else "小"
            deep_cap_adjust_reason = (
                f"盘面尾部{tail_len}连{side_text}，本层暂停上限由 {original_deep_cap} 调整为 {deep_cap}"
            )

    level_label = f"深度风控（{deep_milestone}连输档）"
    model_eval = {
        **risk_pause,
        "level": f"DEEP_{deep_milestone}",
        "level_label": level_label,
    }
    model_pause_rounds, model_reason, model_source = await _suggest_pause_rounds_by_model(
        user_ctx,
        model_eval,
        max_pause=deep_cap,
    )
    initial_amount = int(rt.get("initial_amount", 500) or 500)
    min_pause_rounds = 1
    if deep_milestone >= 6:
        min_pause_rounds = 2
    if initial_amount >= 10000 and deep_milestone >= 3:
        min_pause_rounds = max(min_pause_rounds, 2)
    if initial_amount >= 20000 and deep_milestone >= 6:
        min_pause_rounds = max(min_pause_rounds, 3)
    pause_rounds = max(min_pause_rounds, min(deep_cap, int(model_pause_rounds)))
    _enter_pause(rt, pause_rounds, f"深度风控暂停（{deep_milestone}连输档）")
    rt["risk_pause_snapshot_count"] = settled_count
    rt["risk_pause_block_hits"] = int(rt.get("risk_pause_block_hits", 0)) + 1
    rt["risk_pause_block_rounds"] = int(rt.get("risk_pause_block_rounds", 0)) + pause_rounds

    deep_triggered = _get_deep_triggered_milestones(rt)
    if deep_milestone not in deep_triggered:
        deep_triggered.append(deep_milestone)
    rt["risk_deep_triggered_milestones"] = sorted(set(int(x) for x in deep_triggered))

    wins = risk_pause.get("wins", 0)
    total = risk_pause.get("total", 0)
    win_rate = risk_pause.get("win_rate", 0.0) * 100
    reason_text = "、".join(risk_pause.get("reasons", [])) or f"连输达到{deep_milestone}档位"
    if deep_cap_adjust_reason:
        reason_text = f"{reason_text}；{deep_cap_adjust_reason}"
    resume_hint = _build_pause_resume_hint(rt)
    pause_msg = (
        f"⛔ 自动风控暂停 ⛔\n\n"
        f"触发层级：{level_label}\n"
        f"触发原因：{reason_text}\n"
        f"最近{total}笔胜率：{wins}/{total}（{win_rate:.1f}%）\n"
        f"触发点：第 {next_sequence} 手下注前\n"
        f"模型建议：{model_pause_rounds} 局（来源：{model_source}）\n"
        f"本次暂停：{pause_rounds} 局（该层上限 {deep_cap}，最低保护 {min_pause_rounds} 局，不占基础预算）\n"
        f"模型依据：{model_reason}\n"
        f"暂停期间：保留当前倍投进度，不会重置首注\n"
        f"{resume_hint}"
    )

    if hasattr(user_ctx, "risk_pause_message") and user_ctx.risk_pause_message:
        await cleanup_message(client, user_ctx.risk_pause_message)
    user_ctx.risk_pause_message = await send_to_admin(client, pause_msg, user_ctx, global_config)
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=pause_rounds,
    )
    rt["risk_pause_priority_notified"] = True
    user_ctx.save_state()

    log_event(
        logging.INFO,
        "settle",
        "结算阶段触发深度风控暂停",
        user_id=user_ctx.user_id,
        data=(
            f"milestone={deep_milestone}, next_seq={next_sequence}, "
            f"pause_rounds={pause_rounds}, source={model_source}"
        ),
    )
    return True


async def _handle_goal_pause_after_settle(
    client,
    user_ctx: UserContext,
    global_config: dict,
) -> bool:
    """
    统一处理“炸号/盈利达成”触发的暂停。
    仅做结构收敛，不改变原有阈值与重置语义。
    """
    state = user_ctx.state
    rt = state.runtime

    explode_count = int(rt.get("explode_count", 0))
    explode = int(rt.get("explode", 5))
    period_profit = int(rt.get("period_profit", 0))
    profit_target = int(rt.get("profit", 1000000))

    if not (explode_count >= explode or period_profit >= profit_target):
        return False

    if not rt.get("flag", True):
        return False
    rt["flag"] = False

    notify_type = "explode" if explode_count >= explode else "profit"
    log_event(logging.INFO, 'settle', '触发通知', user_id=user_ctx.user_id, data=f'type={notify_type}')

    if notify_type == "profit":
        date_str = datetime.now().strftime("%m月%d日")
        current_round_str = f"{datetime.now().strftime('%Y%m%d')}_{rt.get('current_round', 1)}"
        round_bet_count = sum(
            1 for entry in state.bet_sequence_log
            if str(entry.get("bet_id", "")).startswith(current_round_str)
        )
        win_msg = _build_ops_card(
            f"😄📈 {date_str}第 {rt.get('current_round', 1)} 轮 赢了",
            summary="本轮已达到盈利条件，系统会按设定进入暂停观察。",
            fields=[
                ("收益", f"{period_profit / 10000:.2f} 万"),
                ("共下注", f"{round_bet_count} 次"),
            ],
            action="建议查看 `status`，确认暂停局数和下一轮状态。",
        )
        await send_message_v2(client, "win", win_msg, user_ctx, global_config)
    else:
        explode_msg = _build_ops_card(
            "💥 本轮炸了",
            summary="当前轮次触发炸号保护，系统会立即暂停观察。",
            fields=[("收益", f"{period_profit / 10000:.2f} 万")],
            action="建议先看 `status`，确认暂停局数与当前资金状态。",
        )
        await send_message_v2(client, "explode", explode_msg, user_ctx, global_config)

    configured_stop_rounds = int(rt.get("stop", 3) if notify_type == "explode" else rt.get("profit_stop", 5))
    pause_reason = "炸号保护暂停" if notify_type == "explode" else "盈利达成暂停"
    _enter_pause(rt, configured_stop_rounds, pause_reason)
    rt["bet_sequence_count"] = 0

    if period_profit >= profit_target:
        rt["current_round"] = int(rt.get("current_round", 1)) + 1
        rt["current_bet_seq"] = 1

    rt["explode_count"] = 0
    rt["period_profit"] = 0
    rt["lose_count"] = 0
    rt["win_count"] = 0
    rt["bet_amount"] = int(rt.get("initial_amount", 500))
    _clear_lose_recovery_tracking(rt)

    resume_hint = _build_pause_resume_hint(rt)
    pause_msg = _build_ops_card(
        f"⛔ {'被炸保护暂停' if notify_type == 'explode' else '盈利达成暂停'} ⛔",
        summary="系统已进入目标暂停，当前策略状态会被保留，不会重置首注。",
        fields=[
            ("原因", "被炸保护" if notify_type == 'explode' else "盈利达成"),
            ("本次暂停", f"{configured_stop_rounds} 局"),
            ("恢复提示", resume_hint),
        ],
        action="建议等待倒计时结束，或执行 `status` 查看剩余暂停局数。",
    )
    log_event(
        logging.INFO,
        'settle',
        '暂停押注',
        user_id=user_ctx.user_id,
        data=f'type={notify_type}, stop_count={configured_stop_rounds}'
    )
    await send_message_v2(
        client,
        "goal_pause",
        pause_msg,
        user_ctx,
        global_config,
        title=f"菠菜机器人 {user_ctx.config.name} {'炸号' if notify_type == 'explode' else '盈利'}暂停",
        desp=pause_msg,
    )
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=configured_stop_rounds,
    )
    return True


def count_consecutive(history):
    """统计连续出现次数 - 与master版本一致"""
    result_counts = {"大": {}, "小": {}}
    if not history:
        return result_counts
    
    current_streak = 1
    for i in range(1, len(history)):
        if history[i] == history[i-1]:
            current_streak += 1
        else:
            key = "大" if history[i-1] == 1 else "小"
            result_counts[key][current_streak] = result_counts[key].get(current_streak, 0) + 1
            current_streak = 1
    
    key = "大" if history[-1] == 1 else "小"
    result_counts[key][current_streak] = result_counts[key].get(current_streak, 0) + 1
    
    return result_counts


def count_lose_streaks(bet_sequence_log):
    """统计连输次数 - 与master版本一致"""
    lose_streaks = {}
    current_streak = 0
    
    for entry in bet_sequence_log:
        profit = entry.get("profit", 0)
        if profit < 0:
            current_streak += 1
        else:
            if current_streak > 0:
                lose_streaks[current_streak] = lose_streaks.get(current_streak, 0) + 1
            current_streak = 0
    
    if current_streak > 0:
        lose_streaks[current_streak] = lose_streaks.get(current_streak, 0) + 1
    
    return lose_streaks


def _clear_lose_recovery_tracking(rt: dict) -> None:
    """清理连输回补跟踪状态，避免跨轮次残留导致误发“连输已终止”消息。"""
    rt["lose_notify_pending"] = False
    rt["lose_start_info"] = {}


def _is_valid_lose_range(start_round, start_seq, end_round, end_seq) -> bool:
    """校验连输区间是否有效（起点不晚于终点）。"""
    try:
        sr = int(start_round)
        ss = int(start_seq)
        er = int(end_round)
        es = int(end_seq)
    except Exception:
        return False

    if sr > er:
        return False
    if sr == er and ss > es:
        return False
    return True


def generate_bet_id(user_ctx: UserContext) -> str:
    """生成押注 ID（与 master 逻辑一致：按天重置轮次）。"""
    rt = user_ctx.state.runtime
    current_date = datetime.now().strftime("%Y%m%d")
    if current_date != rt.get("last_reset_date", ""):
        rt["current_round"] = 1
        rt["current_bet_seq"] = 1
        rt["last_reset_date"] = current_date
    return f"{current_date}_{rt.get('current_round', 1)}_{rt.get('current_bet_seq', 1)}"


def format_bet_id(bet_id):
    """将押注 ID 转换为直观格式，如 '3月14日第 1 轮第 12 次'。"""
    try:
        date_str, round_num, seq_num = str(bet_id).split('_')
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        return f"{month}月{day}日第 {round_num} 轮第 {seq_num} 次"
    except Exception:
        return str(bet_id)


def get_settle_position(state, rt):
    """
    获取当前结算对应的轮次与序号。
    优先用当前结算 bet_id，回退到 current_bet_seq - 1。
    """
    settle_round = int(rt.get("current_round", 1))
    settle_seq = max(1, int(rt.get("current_bet_seq", 1)) - 1)
    if state.bet_sequence_log:
        last_bet_id = str(state.bet_sequence_log[-1].get("bet_id", ""))
        import re
        match = re.match(r"^\d{8}_(\d+)_(\d+)$", last_bet_id)
        if match:
            settle_round = int(match.group(1))
            settle_seq = int(match.group(2))
    return settle_round, settle_seq


def _format_recent_binary(history: list, window: int) -> str:
    """
    格式化最近 N 局结果为二进制字符串
    与 master 版本 _format_recent_binary 一致
    """
    if len(history) < window:
        window = len(history)
    if window <= 0:
        return ""
    recent = history[-window:]
    return "".join(str(x) for x in recent)


def _get_current_streak(history: list):
    """返回当前连串长度与方向（与 master 一致）。"""
    if not history:
        return 0, "大"
    tail = history[-1]
    streak = 1
    for value in reversed(history[:-1]):
        if value == tail:
            streak += 1
        else:
            break
    return streak, ("大" if tail == 1 else "小")


def _compact_reason_text(reason: str, max_len: int = 96) -> str:
    """压缩风控原因，避免在通知里输出超长分析（与 master 一致）。"""
    if not reason:
        return "策略风控触发"
    first_line = str(reason).splitlines()[0].strip()
    return first_line if len(first_line) <= max_len else first_line[: max_len - 1] + "…"


def generate_mobile_bet_report(
    history: list,
    direction: str,
    amount: int,
    sequence_count: int,
    bet_id: str = ""
) -> str:
    streak_len, streak_side = _get_current_streak(history)
    bet_label = format_bet_id(bet_id) if bet_id else "本次"
    return _build_ops_card(
        f"🎯 **{bet_label}押注执行** 🎯",
        summary="本局下注指令已发送，等待结算结果回写。",
        fields=[
            ("😀 连续押注", f"{sequence_count} 次"),
            ("⚡ 押注方向", direction),
            ("💵 押注本金", format_number(amount)),
            (f"📊 当前连{streak_side}", streak_len),
        ],
        action="本局无需额外操作，建议等待结果通知。",
    )


def generate_mobile_pause_report(
    history: list,
    pause_reason: str,
    confidence: float = None,
    entropy: float = None
) -> str:
    streak_len, streak_side = _get_current_streak(history)
    reason_text = _compact_reason_text(pause_reason)
    w5 = _format_recent_binary(history, 5)
    w10 = _format_recent_binary(history, 10)
    w40 = _format_recent_binary(history, 40)

    lines = [
        "⛔ 风控暂停简报 ⛔",
        "",
        f"原因：{reason_text}",
    ]
    if confidence is not None:
        lines.append(f"置信度：{confidence}%")
    if entropy is not None:
        lines.append(f"熵值：{entropy:.2f}")
    lines.extend(
        [
            f"近5局：{w5}",
            f"近10局：{w10}",
            f"近40局：{w40}",
            f"当前连{streak_side}：{streak_len}",
            "动作：暂停下注，继续观察",
        ]
    )
    return "\n".join(lines)


def _build_fund_pause_message(current_fund: int) -> str:
    return _build_ops_card(
        "⛔ 菠菜资金不足，已暂停押注",
        summary="当前资金无法覆盖下一手下注，系统已自动暂停以避免继续扩大风险。",
        fields=[
            ("当前剩余", f"{max(0, int(current_fund or 0)) / 10000:.2f} 万"),
            ("恢复方式", "`gf [金额]`"),
        ],
        action="补充资金后，建议先执行 `status`，确认状态正常再继续。",
    )


def _build_version_catalog_message(result: Dict[str, Any]) -> str:
    current = result.get("current", {})
    current_short = current.get("short_commit", "unknown") or "unknown"
    current_tag_exact = current.get("current_tag", "") or ""
    nearest_tag = current.get("nearest_tag", "") or ""
    if current_tag_exact:
        current_tag_display = current_tag_exact.upper()
    elif nearest_tag:
        current_tag_display = f"无（最近: {nearest_tag}）"
    else:
        current_tag_display = "无"

    remote_head = result.get("remote_head", {}) or {}
    remote_head_short = remote_head.get("short_commit", "-") or "-"
    remote_head_tag = result.get("remote_head_tag", "") or ""
    pending_tags = result.get("pending_tags", [])
    recent_tags = result.get("recent_tags", []) or []
    recent_commits = result.get("recent_commits", []) or []

    latest_tag_target = pending_tags[0] if pending_tags else ""
    if latest_tag_target:
        latest_tag_line = f"{latest_tag_target}（可执行 `update {latest_tag_target}`）"
    else:
        latest_tag_line = "无（已是最新）"

    latest_commit_target = ""
    if remote_head_short not in {"", "-", "unknown"} and remote_head_short != current_short:
        latest_commit_target = remote_head_short

    if latest_commit_target:
        extra_tag_note = f" | Tag:{remote_head_tag}" if remote_head_tag else " | 未打Tag"
        latest_commit_line = f"{latest_commit_target}{extra_tag_note}（可执行 `update {latest_commit_target}`）"
    else:
        latest_commit_line = "无（已是最新）"

    highlights = []
    if recent_tags:
        highlights.append("最近版本：")
        for idx, item in enumerate(recent_tags[:3], 1):
            tag = item.get("tag", "") or "-"
            date = item.get("date", "") or "-"
            summary = item.get("summary", "") or "-"
            highlights.append(f"{idx}. {tag} | {date} | {summary}")
    if recent_commits:
        highlights.append("")
        highlights.append("最近提交：")
        for idx, item in enumerate(recent_commits[:3], 1):
            short_commit = item.get("short_commit", "") or "-"
            date = item.get("date", "") or "-"
            summary = item.get("summary", "") or "-"
            suffix = "（当前）" if short_commit == current_short else ""
            highlights.append(f"{idx}. {short_commit} | {date} | {summary}{suffix}")

    return _build_ops_card(
        "📦 版本信息概览",
        summary="当前版本状态与可更新目标如下。",
        fields=[
            ("当前 Tag", current_tag_display),
            ("当前 Commit", current_short),
            ("最新 Tag", latest_tag_line),
            ("最新 Commit", latest_commit_line),
        ],
        action="需要升级可执行 `update <版本或提交>`；完成后记得执行 `restart`。",
        note="\n".join(highlights).strip(),
    )


async def _process_settle_slim(client, event, user_ctx: UserContext, global_config: dict):
    state = user_ctx.state
    rt = state.runtime
    text = event.message.message

    try:
        match = re.search(r"已结算[^0-9]*(?:结果[为中])?[^0-9]*(\d+)\s*(大|小)", text)
        if not match:
            return

        settle_msg_id = int(getattr(event, "id", 0) or 0)
        last_settle_msg_id = int(rt.get("last_settle_message_id", 0) or 0)
        if settle_msg_id > 0 and settle_msg_id == last_settle_msg_id:
            return
        if settle_msg_id > 0:
            rt["last_settle_message_id"] = settle_msg_id

        result_type = match.group(2)
        is_big = result_type == "大"
        result = 1 if is_big else 0

        try:
            rt["account_balance"] = await fetch_balance(user_ctx)
            rt["balance_status"] = "success"
        except Exception as e:
            log_event(logging.WARNING, 'settle', '鑾峰彇璐︽埛浣欓澶辫触锛屼娇鐢ㄩ粯璁ゅ€?', user_id=user_ctx.user_id, data=str(e))
            rt["balance_status"] = "network_error"

        state.history.append(result)
        state.history = state.history[-2000:]
        lose_end_payload = None

        async def _apply_settle_fund_safety_guard() -> None:
            next_bet_amount = calculate_bet_amount(rt)
            if next_bet_amount <= 0:
                rt["fund_pause_notified"] = False
                return
            if not is_fund_available(user_ctx, next_bet_amount):
                if _sync_fund_from_account_when_insufficient(rt, next_bet_amount):
                    user_ctx.save_state()
                if not is_fund_available(user_ctx, next_bet_amount):
                    if not rt.get("fund_pause_notified", False):
                        display_fund = max(0, rt.get("gambling_fund", 0))
                        mes = _build_fund_pause_message(display_fund)
                        await send_message_v2(
                            client,
                            "fund_pause",
                            mes,
                            user_ctx,
                            global_config,
                            title=f"菠菜机器人 {user_ctx.config.name} 资金暂停",
                            desp=mes,
                        )
                        rt["fund_pause_notified"] = True
                    rt["bet"] = False
                    rt["bet_on"] = False
                    rt["mode_stop"] = True
                else:
                    rt["fund_pause_notified"] = False
            else:
                rt["fund_pause_notified"] = False

        if rt.get("bet", False):
            settled_entry = _get_latest_open_bet_entry(state)
            if settled_entry is None:
                rt["bet"] = False
                user_ctx.save_state()
                return

            prediction = int(rt.get("bet_type", -1))
            win = (is_big and prediction == 1) or (not is_big and prediction == 0)
            bet_amount = int(rt.get("bet_amount", 500))
            profit = int(bet_amount * 0.99) if win else -bet_amount
            settle_round, settle_seq = get_settle_position(state, rt)
            old_lose_count = int(rt.get("lose_count", 0))
            direction = "大" if prediction == 1 else "小"
            result_text = "赢" if win else "输"

            rt["bet"] = False
            state.bet_type_history.append(prediction)
            rt["gambling_fund"] = rt.get("gambling_fund", 0) + profit
            rt["earnings"] = rt.get("earnings", 0) + profit
            rt["period_profit"] = rt.get("period_profit", 0) + profit
            rt["win_total"] = rt.get("win_total", 0) + (1 if win else 0)
            rt["win_count"] = rt.get("win_count", 0) + 1 if win else 0
            rt["lose_count"] = rt.get("lose_count", 0) + 1 if not win else 0
            rt["status"] = 1 if win else 0

            settled_entry["result"] = result_text
            settled_entry["profit"] = profit
            settled_entry["settled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            active_chain_summary = _summarize_effective_bet_chain(state)
            recent_resolved_summary = _summarize_recent_resolved_chain(state)
            if not win:
                rt["bet_sequence_count"] = max(
                    int(active_chain_summary.get("continuous_count", 0)),
                    int(old_lose_count) + 1,
                )
                rt["lose_count"] = max(
                    int(active_chain_summary.get("lose_count", 0)),
                    int(old_lose_count) + 1,
                )
                rt["bet_amount"] = int(active_chain_summary.get("last_amount", bet_amount) or bet_amount)

            if not win:
                if rt.get("lose_count", 0) == 1:
                    _clear_lose_recovery_tracking(rt)
                    rt["lose_start_info"] = {
                        "round": settle_round,
                        "seq": settle_seq,
                        "fund": rt.get("gambling_fund", 0) + bet_amount
                    }
                warning_lose_count = rt.get("warning_lose_count", 3)
                if rt.get("lose_count", 0) >= warning_lose_count:
                    rt["lose_notify_pending"] = True
                    total_losses = int(active_chain_summary.get("total_losses", abs(profit)))
                    warn_msg = _build_ops_card(
                        f"⚠️ {int(rt.get('lose_count', 0))} 连输告警 ⚠️",
                        summary="当前链路已进入高关注状态，请重点关注下一手与账户余额变化。",
                        fields=[
                            ("🔢 时间", f"{datetime.now().strftime('%m月%d日')} 第 {settle_round} 轮第 {settle_seq} 次"),
                            ("📋 预设名称", rt.get('current_preset_name', 'none')),
                            ("😀 连续押注", f"{int(active_chain_summary.get('continuous_count', rt.get('bet_sequence_count', 0)))} 次"),
                            ("⚡ 押注方向", direction),
                            ("💵 押注本金", format_number(bet_amount)),
                            ("💰 累计损失", format_number(total_losses)),
                            ("💰 账户余额", f"{rt.get('account_balance', 0) / 10000:.2f} 万"),
                            ("💰 菠菜余额", f"{rt.get('gambling_fund', 0) / 10000:.2f} 万"),
                        ],
                        action="建议立即查看 `status`；如不准备继续，可直接执行 `pause`。",
                    )
                    if hasattr(user_ctx, "lose_streak_message") and user_ctx.lose_streak_message:
                        await cleanup_message(client, user_ctx.lose_streak_message)
                    user_ctx.lose_streak_message = await send_message_v2(
                        client,
                        "lose_streak",
                        warn_msg,
                        user_ctx,
                        global_config,
                        title=f"菠菜机器人 {user_ctx.config.name} 连输告警",
                        desp=warn_msg
                    )

            if win and rt.get("lose_notify_pending", False):
                warning_lose_count = int(rt.get("warning_lose_count", 3))
                lose_start_info = rt.get("lose_start_info", {})
                start_round = lose_start_info.get("round", "?")
                start_seq = lose_start_info.get("seq", "?")
                end_round = settle_round
                end_seq = settle_seq
                total_profit = rt.get("gambling_fund", 0) - lose_start_info.get("fund", rt.get("gambling_fund", 0))
                total_loss = int(recent_resolved_summary.get("total_losses", 0))
                current_balance = int(rt.get("account_balance", 0) or 0)
                current_fund = int(rt.get("gambling_fund", 0) or 0)
                if int(old_lose_count) >= warning_lose_count and _is_valid_lose_range(start_round, start_seq, end_round, end_seq):
                    continuous_count = max(
                        int(recent_resolved_summary.get("continuous_count", 0)),
                        int(old_lose_count) + 1,
                    )
                    lose_end_payload = {
                        "start_round": start_round,
                        "start_seq": start_seq,
                        "end_round": end_round,
                        "end_seq": end_seq,
                        "lose_count": old_lose_count,
                        "continuous_count": continuous_count,
                        "total_loss": total_loss,
                        "total_profit": total_profit,
                        "account_balance": current_balance,
                        "gambling_fund": current_fund,
                    }
                _clear_lose_recovery_tracking(rt)
            elif win:
                _clear_lose_recovery_tracking(rt)

            user_ctx.save_state()

            result_amount = format_number(int(bet_amount * 0.99) if win else bet_amount)
            last_bet_id = settled_entry.get("bet_id", "") if isinstance(settled_entry, dict) else ""
            bet_id = format_bet_id(last_bet_id) if last_bet_id else f"{datetime.now().strftime('%m月%d日')}第 {rt.get('current_round', 1)} 轮第 {rt.get('current_bet_seq', 1)} 次"
            settle_sequence_count = int(recent_resolved_summary.get("continuous_count", rt.get("bet_sequence_count", 0)))

            mes = _build_ops_card(
                f"🔢 {bet_id}押注结果 🔢",
                summary="本局已完成结算，状态和资金已同步更新。",
                fields=[
                    ("😀 连续押注", f"{settle_sequence_count} 次"),
                    ("⚡ 押注方向", direction),
                    ("💵 押注本金", format_number(bet_amount)),
                    ("📉 输赢结果", f"{result_text} {result_amount}"),
                    ("🎲 开奖结果", result_type),
                    ("🤖 预测依据", rt.get('last_predict_info', 'N/A')),
                ],
                action="如需继续观察，等待下一次盘口；如需复核当前状态，请执行 `status`。",
            )
            await send_to_admin(client, mes, user_ctx, global_config)

            if win or rt.get("lose_count", 0) >= rt.get("lose_stop", 13):
                rt["bet_sequence_count"] = 0
                rt["bet_amount"] = int(rt.get("initial_amount", 500))

        await _apply_settle_fund_safety_guard()

        if len(state.history) % 5 == 0:
            user_ctx.save_state()

        await _handle_goal_pause_after_settle(client, user_ctx, global_config)

        if hasattr(user_ctx, 'dashboard_message') and user_ctx.dashboard_message:
            await cleanup_message(client, user_ctx.dashboard_message)
        await _refresh_dashboard_message_slim(client, user_ctx, global_config)

        current_total = int(rt.get("total", 0))
        last_stats_total = int(rt.get("stats_last_report_total", 0))
        if (
            len(state.history) > 5
            and current_total > 0
            and current_total % AUTO_STATS_INTERVAL_ROUNDS == 0
            and current_total != last_stats_total
        ):
            windows = [1000, 500, 200, 100]
            stats = {"连大": [], "连小": [], "连输": []}
            all_ns = set()

            for window in windows:
                history_window = state.history[-window:]
                result_counts = count_consecutive(history_window)
                bet_sequence_log = state.bet_sequence_log[-window:]
                lose_streaks = count_lose_streaks(bet_sequence_log)
                stats["连大"].append(result_counts["大"])
                stats["连小"].append(result_counts["小"])
                stats["连输"].append(lose_streaks)
                all_ns.update(result_counts["大"].keys())
                all_ns.update(result_counts["小"].keys())
                all_ns.update(lose_streaks.keys())

            mes_lines = ["```", "最近局数“连大、连小、连输”统计", ""]
            for category in ["连大", "连小", "连输"]:
                mes_lines.append(category)
                mes_lines.append("================================")
                mes_lines.append("类别 | 1000|  500  |200 | 100|")
                mes_lines.append("--------------------------------")
                for n in sorted(all_ns, reverse=True):
                    if any(n in stats[category][i] for i in range(len(windows))):
                        row = f" {str(n).center(2)}  |"
                        for i in range(len(windows)):
                            count = stats[category][i].get(n, 0)
                            value = str(count) if count > 0 else "-"
                            row += f" {value.center(3)} |"
                        mes_lines.append(row)
                mes_lines.append("")
            mes_lines.append("```")
            mes = "\n".join(mes_lines)
            stats_message = await send_to_admin(client, mes, user_ctx, global_config)
            user_ctx.stats_message = stats_message
            rt["stats_last_report_total"] = current_total
            if stats_message:
                asyncio.create_task(delete_later(client, stats_message.chat_id, stats_message.id, AUTO_STATS_DELETE_DELAY_SECONDS))

        if lose_end_payload:
            date_str = datetime.now().strftime("%m月%d日")
            start_round = lose_end_payload.get("start_round", "?")
            start_seq = lose_end_payload.get("start_seq", "?")
            end_round = lose_end_payload.get("end_round", "?")
            end_seq = lose_end_payload.get("end_seq", "?")
            lose_count = int(lose_end_payload.get("lose_count", 0))
            if str(start_round) == str(end_round):
                range_text = f"{date_str} 第 {start_round} 轮第 {start_seq} 次 至 第 {end_seq} 次"
            else:
                range_text = f"{date_str} 第 {start_round} 轮第 {start_seq} 次 至 第 {end_round} 轮第 {end_seq} 次"
            rec_msg = _build_ops_card(
                f"✅ {lose_count} 连输已终止！ ✅",
                summary="本轮回补已经结束，系统已回写收益与当前余额。",
                fields=[
                    ("🔢 时间", range_text),
                    ("📋 预设名称", rt.get('current_preset_name', 'none')),
                    ("😀 连续押注", f"{lose_end_payload.get('continuous_count', lose_count)} 次"),
                    ("⚠️本局连输", f" {lose_count} 次"),
                    ("💰 本局盈利", f" {format_number(lose_end_payload.get('total_profit', 0))}"),
                    ("💰 账户余额", f"{lose_end_payload.get('account_balance', rt.get('account_balance', 0)) / 10000:.2f} 万"),
                    ("💰 菠菜资金剩余", f"{lose_end_payload.get('gambling_fund', rt.get('gambling_fund', 0)) / 10000:.2f} 万"),
                ],
                action="建议关注是否已回到首注，并继续观察下一次盘口。",
            )
            if hasattr(user_ctx, "lose_streak_message") and user_ctx.lose_streak_message:
                await cleanup_message(client, user_ctx.lose_streak_message)
                user_ctx.lose_streak_message = None
            await send_message_v2(client, "lose_end", rec_msg, user_ctx, global_config)
    except Exception as e:
        log_event(logging.ERROR, 'settle', '结算失败', user_id=user_ctx.user_id, data=str(e))
        await send_to_admin(
            client,
            _build_ops_card(
                "❌ 结算处理失败",
                summary="本次结算回写没有完成。",
                fields=[("错误", str(e)[:180])],
                action="建议稍后关注下一条结果；如持续异常，请执行 `status` 或 `restart`。",
            ),
            user_ctx,
            global_config,
        )


async def process_settle(client, event, user_ctx: UserContext, global_config: dict):
    return await _process_settle_slim(client, event, user_ctx, global_config)


async def delete_later(client, chat_id, message_id, delay=10):
    """延迟指定秒数后删除消息。"""
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass


async def handle_model_command_multiuser(event, args, user_ctx: UserContext, global_config: dict):
    """处理 model 命令 - 与master版本handle_model_command一致"""
    rt = user_ctx.state.runtime
    sub_cmd = args[0] if args else "list"
    
    # 兼容 "model id list" 和 "model id XX"
    if sub_cmd == "id":
        if len(args) < 2:
            sub_cmd = "list"
        elif args[1] == "list":
            sub_cmd = "list"
        else:
            sub_cmd = "select"
            args = ["select", args[1]]

    if sub_cmd == "list":
        models = user_ctx.config.ai.get("models", {})
        entries = []
        idx = 1
        current_model_id = rt.get("current_model_id", "")
        
        for k, m in models.items():
            if m.get("enabled", True):
                current = "（当前）" if m.get('model_id') == current_model_id else ""
                entries.append(f"{idx}. `{m.get('model_id', 'unknown')}` {current}".strip())
                idx += 1
        await _reply_ops_card(
            event,
            "🤖 可用模型列表",
            summary="以下是当前账号可用的模型。",
            fields=[("模型", "\n".join(entries) if entries else "暂无可用模型")],
            action="切换模型可执行 `model select <编号或ID>`。",
        )
        
    elif sub_cmd in ["select", "use", "switch"]:
        if len(args) < 2:
            await _reply_ops_card(
                event,
                "❌ 缺少模型目标",
                summary="当前没有提供要切换的模型编号或 ID。",
                action="请执行 `model select 1` 或 `model select qwen3-coder-plus`。",
            )
            return
            
        target_id = args[1]
        models = user_ctx.config.ai.get("models", {})
        
        # 支持数字编号选择
        if target_id.isdigit():
            idx = int(target_id)
            enabled_models = [m for m in models.values() if m.get("enabled", True)]
            if 1 <= idx <= len(enabled_models):
                target_id = enabled_models[idx-1].get('model_id', '')
            else:
                await _reply_ops_card(
                    event,
                    "❌ 模型编号无效",
                    summary=f"编号 {idx} 不在当前可选范围内。",
                    action="请先执行 `model list` 查看可用编号。",
                )
                return
        
        # 验证模型是否存在
        model_exists = any(m.get('model_id') == target_id for m in models.values() if m.get("enabled"))
        if not model_exists:
            await _reply_ops_card(
                event,
                "❌ 模型不可用",
                summary=f"模型 `{target_id}` 不存在或当前未启用。",
                action="请先执行 `model list` 确认可用模型。",
            )
            return
            
        await _reply_ops_card(
            event,
            "🔄 正在切换模型",
            summary="系统正在切换默认模型。",
            fields=[("目标模型", f"`{target_id}`")],
            action="请等待切换结果返回。",
        )
        
        # 切换模型
        rt["current_model_id"] = target_id
        user_ctx.save_state()
        
        success_msg = _build_ops_card(
            "✅ 模型切换成功",
            summary="后续新局会使用这个模型继续判断。",
            fields=[
                ("当前模型", f"`{target_id}`"),
                ("连接状态", "正常"),
            ],
            action="建议等待下一局生效，或执行 `status` 查看当前概览。",
        )
        await event.reply(success_msg)
        log_event(logging.INFO, 'model', '切换模型', user_id=user_ctx.user_id, model=target_id)
            
    elif sub_cmd == "reload":
        await _reply_ops_card(
            event,
            "🔄 重新加载模型配置",
            summary="系统正在重新读取当前账号的模型配置。",
            action="请等待结果返回。",
        )
        try:
            user_ctx.reload_user_config()
            model_mgr = user_ctx.get_model_manager()
            model_mgr.load_models()
            models = model_mgr.list_models()
            enabled_count = sum(
                1
                for provider_models in models.values()
                for model in provider_models
                if model.get("enabled", True)
            )
            log_event(logging.INFO, 'model', '重新加载模型', user_id=user_ctx.user_id, enabled=enabled_count)
            await _reply_ops_card(
                event,
                "✅ 模型配置已重新加载",
                summary="模型配置刷新完成。",
                fields=[("可用模型", enabled_count)],
                action="如需切换，请执行 `model select <编号或ID>`。",
            )
        except Exception as e:
            log_event(logging.ERROR, 'model', '重载模型配置失败', user_id=user_ctx.user_id, error=str(e))
            await _reply_ops_card(
                event,
                "❌ 模型配置重载失败",
                summary="本次重载没有完成。",
                fields=[("错误", str(e)[:120])],
                action="建议检查账号配置文件后再重试。",
            )
    else:
        await _reply_ops_card(
            event,
            "❓ 未知模型命令",
            summary="当前子命令无法识别。",
            fields=[("用法", "`model list`\n`model select <id>`\n`model reload`")],
            action="建议先执行 `model list` 查看当前可用模型。",
        )


async def handle_apikey_command_multiuser(event, args, user_ctx: UserContext):
    """处理 apikey 命令：show/set/add/del/test。"""
    rt = user_ctx.state.runtime
    sub_cmd = (args[0].lower() if args else "show")
    ai_cfg = user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {}
    keys = _normalize_ai_keys(ai_cfg)

    if sub_cmd in ("show", "list", "ls"):
        if not keys:
            await _reply_ops_card(
                event,
                "🔐 当前未配置 AI key",
                summary="当前账号还没有可用的模型密钥。",
                action="请执行 `apikey set <新key>`。",
            )
            return
        lines = []
        for idx, key in enumerate(keys, 1):
            lines.append(f"{idx}. `{_mask_api_key(key)}`")
        await _reply_ops_card(
            event,
            "🔐 当前账号 AI key 列表",
            summary="已按脱敏方式展示，避免在聊天窗口泄露完整 key。",
            fields=[("Key", "\n".join(lines))],
            action="可执行 `apikey set` / `apikey add` / `apikey del` / `apikey test`。",
        )
        return

    if sub_cmd in ("set", "add"):
        if len(args) < 2:
            await _reply_ops_card(
                event,
                "❌ 缺少 key 参数",
                summary="当前没有提供新的 key。",
                action=f"请执行 `apikey {sub_cmd} <新key>`。",
            )
            return

        new_key = str(args[1]).strip()
        if not new_key:
            await _reply_ops_card(
                event,
                "❌ key 不能为空",
                summary="当前输入的 key 为空。",
                action=f"请重新执行 `apikey {sub_cmd} <新key>`。",
            )
            return

        if sub_cmd == "set":
            updated_keys = [new_key]
        else:
            updated_keys = list(keys)
            if new_key in updated_keys:
                await _reply_ops_card(
                    event,
                    "⚠️ 无需重复添加",
                    summary="该 key 已经存在于当前账号配置中。",
                    action="如需覆盖全部 key，请使用 `apikey set <新key>`。",
                )
                return
            updated_keys.append(new_key)

        new_ai = dict(ai_cfg)
        new_ai["api_keys"] = updated_keys
        new_ai.pop("api_key", None)
        try:
            config_path = user_ctx.update_ai_config(new_ai)
            _clear_ai_key_issue(rt)
            user_ctx.save_state()
            model_mgr = user_ctx.get_model_manager()
            model_mgr.load_models()
            await _reply_ops_card(
                event,
                "✅ AI key 已更新",
                summary="新的 key 已写入配置并重新加载。",
                fields=[
                    ("文件", f"`{os.path.basename(config_path)}`"),
                    ("当前 key 数量", len(updated_keys)),
                ],
                action="如需确认可用性，建议继续执行 `apikey test`。",
            )
        except Exception as e:
            log_event(logging.ERROR, 'apikey', '写入 key 失败', user_id=user_ctx.user_id, error=str(e))
            await _reply_ops_card(
                event,
                "❌ AI key 更新失败",
                summary="本次写入配置没有完成。",
                fields=[("错误", str(e)[:160])],
                action="建议检查配置文件权限后再重试。",
            )
        return

    if sub_cmd in ("del", "rm", "remove"):
        if len(args) < 2:
            await _reply_ops_card(
                event,
                "❌ 缺少删除序号",
                summary="当前没有提供要删除的 key 序号。",
                action="请执行 `apikey del <序号>`。",
            )
            return
        try:
            idx = int(str(args[1]).strip())
        except ValueError:
            await _reply_ops_card(
                event,
                "❌ 序号格式错误",
                summary="删除序号必须是整数。",
                action="请执行 `apikey del <序号>`。",
            )
            return

        if idx < 1 or idx > len(keys):
            await _reply_ops_card(
                event,
                "❌ 序号超出范围",
                summary=f"当前 key 数量只有 {len(keys)} 个。",
                action="请先执行 `apikey show` 查看当前序号。",
            )
            return

        updated_keys = list(keys)
        updated_keys.pop(idx - 1)
        new_ai = dict(ai_cfg)
        new_ai["api_keys"] = updated_keys
        new_ai.pop("api_key", None)
        try:
            config_path = user_ctx.update_ai_config(new_ai)
            if not updated_keys:
                _mark_ai_key_issue(rt, "管理员删除了全部 key")
            user_ctx.save_state()
            await _reply_ops_card(
                event,
                "✅ AI key 已删除",
                summary=f"第 {idx} 个 key 已从当前账号配置中移除。",
                fields=[
                    ("文件", f"`{os.path.basename(config_path)}`"),
                    ("剩余 key 数量", len(updated_keys)),
                ],
                action="如需确认当前可用 key，请执行 `apikey show`。",
            )
        except Exception as e:
            log_event(logging.ERROR, 'apikey', '删除 key 失败', user_id=user_ctx.user_id, error=str(e))
            await _reply_ops_card(
                event,
                "❌ AI key 删除失败",
                summary="本次删除没有完成。",
                fields=[("错误", str(e)[:160])],
                action="建议检查配置文件权限后再重试。",
            )
        return

    if sub_cmd in ("test", "check"):
        model_id = rt.get("current_model_id", "qwen3-coder-plus")
        try:
            result = await user_ctx.get_model_manager().validate_model(model_id)
            if result.get("success"):
                _clear_ai_key_issue(rt)
                user_ctx.save_state()
                await _reply_ops_card(
                    event,
                    "✅ 模型测试成功",
                    summary="当前模型连通性正常，可继续使用。",
                    fields=[
                        ("模型", f"`{model_id}`"),
                        ("延迟", f"{result.get('latency', '-')}ms"),
                    ],
                    action="如需切换，请执行 `model select <编号或ID>`。",
                )
            else:
                err = str(result.get("error", "unknown"))
                if _looks_like_ai_key_issue(err):
                    _mark_ai_key_issue(rt, err)
                    user_ctx.save_state()
                await _reply_ops_card(
                    event,
                    "❌ 模型测试失败",
                    summary="当前模型未通过可用性检测。",
                    fields=[
                        ("模型", f"`{model_id}`"),
                        ("错误", err[:180]),
                    ],
                    action="建议先检查 key 或网络，再重新执行 `apikey test`。",
                )
        except Exception as e:
            await _reply_ops_card(
                event,
                "❌ 模型测试失败",
                summary="测试过程中发生异常。",
                fields=[("错误", str(e)[:180])],
                action="建议稍后重试；若持续失败，可检查配置或网络。",
            )
        return

    await _reply_ops_card(
        event,
        "❓ 未知 key 命令",
        summary="当前子命令无法识别。",
        fields=[("用法", "`apikey show`\n`apikey set <key>`\n`apikey add <key>`\n`apikey del <序号>`\n`apikey test`")],
        action="建议先执行 `apikey show` 查看当前状态。",
    )


async def process_user_command(client, event, user_ctx: UserContext, global_config: dict):
    """处理用户命令。"""
    state = user_ctx.state
    rt = state.runtime
    presets = user_ctx.presets
    
    text = event.raw_text.strip()
    if not text:
        return

    my = text.split()
    if not my:
        return

    raw_cmd = str(my[0]).strip()
    if not raw_cmd:
        return

    # 仅解析“命令形态”文本，避免把通知正文(⚠️/🔢/📊开头)当成未知命令。
    # 兼容 `/help` 与中文命令别名 `暂停/恢复`。
    normalized_cmd = raw_cmd[1:] if raw_cmd.startswith("/") else raw_cmd
    if not normalized_cmd:
        return

    allowed_cn_cmds = {"暂停", "恢复"}
    is_ascii_cmd = (
        normalized_cmd[0].isalpha()
        and all(ch.isalnum() or ch in {"_", "-"} for ch in normalized_cmd)
    )
    if normalized_cmd not in allowed_cn_cmds and not is_ascii_cmd:
        return

    cmd = normalized_cmd.lower()
    
    safe_log_text = text[:50]
    if cmd in {"apikey", "ak"}:
        safe_log_text = f"{raw_cmd} ***"
    masked_text, was_masked = _mask_command_text(text)
    append_interaction_event(
        user_ctx,
        direction="inbound",
        kind="command",
        channel="admin_chat",
        text=masked_text,
        command=cmd,
        masked=was_masked,
        chat_id=getattr(event, "chat_id", None),
        message_id=getattr(event, "id", None),
    )
    log_event(logging.INFO, 'user_cmd', '处理用户命令', user_id=user_ctx.user_id, data=safe_log_text)
    
    try:
        # ========== help命令 ==========
        if cmd == "help":
            mes = _build_ops_card(
                "📘 命令列表",
                summary="新手建议先掌握 5 个高频命令：`st`、`status`、`pause`、`resume`、`balance`。",
                fields=[
                    ("基础控制", "`st [预设名]` / `pause` / `resume` / `open` / `off`"),
                    ("参数设置", "`gf [金额]` / `set [炸] [赢] [停] [盈停]` / `warn [次数]` / `wlc [次数]`"),
                    ("模型与策略", "`model [list|select|reload]` / `apikey [show|set|add|del|test]` / `ms [模式]`"),
                    ("测算功能", "`yc [预设名]` / `yc [参数...]`"),
                    ("数据管理", "`res tj` / `res state` / `res bet` / `explain` / `stats` / `balance` / `xx`"),
                    ("发布更新", "`ver` / `update [版本|提交]` / `reback [版本|提交]` / `restart`"),
                    ("预设管理", "`ys [名] ...` / `yss` / `yss dl [名]`"),
                    ("多用户管理", "`users` / `status`"),
                ],
                action="如果只是日常使用，优先看 `status`，启动用 `st`，遇到异常先 `pause`。",
            )
            log_event(logging.INFO, 'user_cmd', '显示帮助', user_id=user_ctx.user_id)
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        # open/off 兼容旧命令：分别等同 resume/pause。
        # 为避免命令歧义，open/off 不再携带额外副作用（如自动发送 /ydx）。
        if cmd == "open":
            cmd = "resume"
        elif cmd == "off":
            cmd = "pause"

        if cmd == "xx":
            target_groups = []
            target_groups.extend(_iter_targets(user_ctx.config.groups.get("zq_group", [])))

            # 去重并保持顺序
            unique_groups = []
            seen = set()
            for gid in target_groups:
                key = str(gid)
                if key in seen:
                    continue
                seen.add(key)
                unique_groups.append(gid)

            if not unique_groups:
                message = await send_to_admin(
                    client,
                    _build_ops_card(
                        "⚠️ 未配置可清理群组",
                        summary="当前账号没有配置 `zq_group`，因此无法执行群消息清理。",
                        action="如需使用 `xx`，请先在账号配置里补充 `zq_group`。",
                    ),
                    user_ctx,
                    global_config,
                )
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
                return

            deleted_total = 0
            failed_groups = []
            scanned_groups = 0

            for gid in unique_groups:
                try:
                    msg_ids = [msg.id async for msg in client.iter_messages(gid, from_user="me", limit=500)]
                    scanned_groups += 1
                    if msg_ids:
                        await client.delete_messages(gid, msg_ids)
                        deleted_total += len(msg_ids)
                except Exception as e:
                    failed_groups.append(f"{gid}: {str(e)[:40]}")

            mes = (
                _build_ops_card(
                    "🧹 群组消息已清理",
                    summary="已按当前配置扫描并清理我发送的历史消息。",
                    fields=[
                        ("扫描群组", scanned_groups),
                        ("删除消息", deleted_total),
                    ],
                    action="如需再次清理，可重新执行 `xx`。",
                    note="\n".join(failed_groups[:5]) if failed_groups else "",
                )
            )

            log_event(
                logging.INFO,
                'user_cmd',
                '执行xx清理',
                user_id=user_ctx.user_id,
                groups=scanned_groups,
                deleted=deleted_total,
                failed=len(failed_groups),
            )
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return

        # pause/resume - 暂停/恢复押注
        if cmd in ("pause", "暂停"):
            if rt.get("manual_pause", False):
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "⏸️ 当前账号已是暂停状态",
                        summary="系统已处于手动暂停，无需重复执行。",
                        action="如需继续，请执行 `resume`；如需查看详情，请执行 `status`。",
                    ),
                    user_ctx,
                    global_config,
                )
                return
            await _clear_pause_countdown_notice(client, user_ctx)
            rt["switch"] = True
            rt["bet_on"] = False
            rt["bet"] = False
            rt["mode_stop"] = True
            rt["manual_pause"] = True
            _clear_lose_recovery_tracking(rt)
            user_ctx.save_state()
            mes = _build_ops_card(
                "⏸️ 已暂停当前账号押注",
                summary="当前账号后续不会自动下注，已有状态会被保留。",
                action="如需恢复，请执行 `resume`；如需查看当前链路，请执行 `status`。",
            )
            await send_to_admin(client, mes, user_ctx, global_config)
            log_event(logging.INFO, 'user_cmd', '暂停押注', user_id=user_ctx.user_id)
            return
        
        if cmd in ("resume", "恢复"):
            await _clear_pause_countdown_notice(client, user_ctx)
            rt["switch"] = True
            rt["bet_on"] = True
            rt["bet"] = False
            rt["mode_stop"] = True
            rt["manual_pause"] = False
            user_ctx.save_state()
            mes = _build_ops_card(
                "▶️ 已恢复当前账号押注",
                summary="后续会继续等待有效盘口触发，不会立即补发历史下注。",
                action="建议执行 `status` 确认当前状态，并等待下一次盘口。",
            )
            await send_to_admin(client, mes, user_ctx, global_config)
            log_event(logging.INFO, 'user_cmd', '恢复押注', user_id=user_ctx.user_id)
            return

        # risk - 基础/深度风控开关
        # st - 启动预设 - 与master一致
        if cmd == "st" and len(my) > 1:
            preset_name = my[1]
            if preset_name in presets:
                preset = presets[preset_name]
                rt["continuous"] = int(preset[0])
                rt["lose_stop"] = int(preset[1])
                rt["lose_once"] = float(preset[2])
                rt["lose_twice"] = float(preset[3])
                rt["lose_three"] = float(preset[4])
                rt["lose_four"] = float(preset[5])
                rt["initial_amount"] = int(preset[6])
                rt["current_preset_name"] = preset_name
                rt["bet_amount"] = int(preset[6])
                await _clear_pause_countdown_notice(client, user_ctx)
                rt["switch"] = True
                rt["manual_pause"] = False
                rt["bet_on"] = True
                rt["mode_stop"] = True
                rt["bet"] = False  # st 命令不直接设置 bet=True，等待真实盘口触发下注
                rt["risk_deep_triggered_milestones"] = []
                rt["fund_pause_notified"] = False
                rt["limit_stop_notified"] = False
                _clear_lose_recovery_tracking(rt)
                user_ctx.save_state()
                
                mes = _build_ops_card(
                    f"🎯 预设启动成功: {preset_name}",
                    summary="当前账号已经切换到新的预设，后续将按这套参数进入可下注状态。",
                    fields=[
                        ("策略参数", f"{preset[0]} {preset[1]} {preset[2]} {preset[3]} {preset[4]} {preset[5]} {preset[6]}"),
                    ],
                    action="建议留意本轮自动测算结果，并用 `status` 确认当前状态。",
                )
                log_event(logging.INFO, 'user_cmd', '启动预设', user_id=user_ctx.user_id, preset=preset_name)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
                await yc_command_handler_multiuser(
                    client,
                    event,
                    [preset_name],
                    user_ctx,
                    global_config,
                    auto_trigger=True,
                )
            else:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 预设不存在",
                        summary=f"当前账号没有找到名为 `{preset_name}` 的预设。",
                        action="请先执行 `yss` 查看可用预设，或用 `ys` 新建一个预设。",
                    ),
                    user_ctx,
                    global_config,
                )
            return
        
        # stats - 查看连大、连小、连输统计
        if cmd == "stats":
            if len(state.history) < 10:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "📉 暂无法生成统计",
                        summary="当前历史数据不足 10 局，统计结果参考意义不够。",
                        action="建议再观察几局后执行 `stats`。",
                    ),
                    user_ctx,
                    global_config,
                )
                return
            
            windows = [1000, 500, 200, 100]
            stats = {"连大": [], "连小": [], "连输": []}
            all_ns = set()
            
            for window in windows:
                history_window = state.history[-window:]
                result_counts = count_consecutive(history_window)
                bet_sequence_log = state.bet_sequence_log[-window:]
                lose_streaks = count_lose_streaks(bet_sequence_log)
                
                stats["连大"].append(result_counts["大"])
                stats["连小"].append(result_counts["小"])
                stats["连输"].append(lose_streaks)
                
                all_ns.update(result_counts["大"].keys())
                all_ns.update(result_counts["小"].keys())
                all_ns.update(lose_streaks.keys())
            
            mes = "```\n最近局数“连大、连小、连输”统计\n\n"
            for category in ["连大", "连小", "连输"]:
                mes += f"{category}\n"
                mes += "================================\n"
                mes += "类别 | 1000|  500  |200 | 100|\n"
                mes += "--------------------------------\n"
                sorted_ns = sorted(all_ns, reverse=True)
                for n in sorted_ns:
                    if any(n in stats[category][i] for i in range(len(windows))):
                        mes += f" {str(n).center(2)}  |"
                        for i in range(len(windows)):
                            count = stats[category][i].get(n, 0)
                            value = str(count) if count > 0 else "-"
                            mes += f" {value.center(3)} |"
                        mes += "\n"
                mes += "\n"
            mes += "```"
            
            log_event(logging.INFO, 'user_cmd', '查看统计', user_id=user_ctx.user_id)
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 30))
            return
        
        # status - 查看仪表盘 - 与master一致
        if cmd == "status":
            dashboard = format_dashboard(user_ctx)
            message = await send_to_admin(client, dashboard, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        # ========== 参数设置命令 ==========
        # gf - 设置资金 - 与master一致
        if cmd == "gf":
            old_fund = rt.get("gambling_fund", 0)
            if len(my) == 1:
                rt["gambling_fund"] = rt.get("gambling_fund", 2000000)
                mes = _build_ops_card(
                    "✅ 菠菜资金已重置",
                    summary="当前账号的菠菜资金已恢复为默认值。",
                    fields=[("当前金额", f"{rt['gambling_fund'] / 10000:.2f} 万")],
                    action="建议执行 `status` 确认资金与状态是否符合预期。",
                )
            elif len(my) == 2:
                try:
                    new_fund = int(my[1])
                    if new_fund < 0:
                        mes = _build_ops_card(
                            "❌ 菠菜资金设置失败",
                            summary="菠菜资金不能设置为负数。",
                            action="请执行 `gf [金额]`，金额必须是大于等于 0 的整数。",
                        )
                    else:
                        account_balance = rt.get("account_balance", 0)
                        if new_fund > account_balance:
                            new_fund = account_balance
                            mes = _build_ops_card(
                                "⚠️ 菠菜资金已自动调整",
                                summary="输入金额超过当前账户余额，系统已自动压到可用上限。",
                                fields=[("当前金额", f"{new_fund / 10000:.2f} 万")],
                                action="建议执行 `balance` 或 `status` 再确认余额状态。",
                            )
                        else:
                            mes = _build_ops_card(
                                "✅ 菠菜资金已更新",
                                summary="新的菠菜资金已经写入当前账号状态。",
                                fields=[("当前金额", f"{new_fund / 10000:.2f} 万")],
                                action="建议执行 `status` 确认后续下一手金额是否符合预期。",
                            )
                        rt["gambling_fund"] = new_fund
                except ValueError:
                    mes = _build_ops_card(
                        "❌ 菠菜资金设置失败",
                        summary="金额格式无效，必须是整数。",
                        action="请执行 `gf [金额]`，例如 `gf 1000000`。",
                    )
            else:
                mes = _build_ops_card(
                    "❌ 菠菜资金命令格式错误",
                    summary="当前命令参数数量不正确。",
                    action="正确用法：`gf` 或 `gf [金额]`。",
                )
            
            log_event(logging.INFO, 'user_cmd', '设置资金', user_id=user_ctx.user_id, mes=mes)
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            
            if rt.get("gambling_fund", 0) != old_fund:
                log_event(logging.INFO, 'user_cmd', '资金变更', user_id=user_ctx.user_id, 
                         old=old_fund, new=rt.get("gambling_fund", 0))
                await check_bet_status(client, user_ctx, global_config)
            return
        
        # set - 设置风控参数 - 与master一致
        if cmd == "set" and len(my) >= 5:
            try:
                rt["explode"] = int(my[1])
                rt["profit"] = int(my[2])
                rt["stop"] = int(my[3])
                rt["profit_stop"] = int(my[4])
                if len(my) > 5:
                    rt["stop_count"] = int(my[5])
                user_ctx.save_state()
                mes = _build_ops_card(
                    "✅ 风控参数已更新",
                    summary="新的炸号、盈利和暂停参数已经写入当前账号状态。",
                    fields=[
                        ("炸号阈值", f"{rt['explode']} 次"),
                        ("盈利目标", f"{rt['profit']/10000:.2f} 万"),
                        ("暂停局数", f"{rt['stop']} 局"),
                        ("盈停局数", f"{rt['profit_stop']} 局"),
                    ],
                    action="建议执行 `status` 复核当前参数是否符合预期。",
                )
                log_event(logging.INFO, 'user_cmd', '设置参数', user_id=user_ctx.user_id,
                         explode=rt['explode'], profit=rt['profit'], stop=rt['stop'], profit_stop=rt['profit_stop'])
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            except ValueError:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 风控参数设置失败",
                        summary="参数格式无效，当前只支持整数。",
                        action="请按 `set [炸] [赢] [停] [盈停]` 重新输入。",
                    ),
                    user_ctx,
                    global_config,
                )
            return
        if cmd == "set":
            await send_to_admin(
                client,
                _build_ops_card(
                    "❌ 风控参数设置失败",
                    summary="当前参数数量不足。",
                    action="请按 `set [炸] [赢] [停] [盈停]` 重新输入完整参数。",
                ),
                user_ctx,
                global_config,
            )
            return

        # warn/wlc - 设置连输告警阈值 - 与master一致
        if cmd in ("warn", "wlc"):
            if len(my) > 1:
                try:
                    warning_count = int(my[1])
                    if warning_count < 1:
                        raise ValueError
                    rt["warning_lose_count"] = warning_count
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 连输告警阈值已更新",
                        summary="后续达到该连输次数时，系统会发出高优提醒。",
                        fields=[("当前阈值", f"{warning_count} 次")],
                        action="建议结合 `status` 观察当前链路压力。",
                    )
                    log_event(logging.INFO, 'user_cmd', '设置连输告警阈值', user_id=user_ctx.user_id, warning_lose_count=warning_count)
                except ValueError:
                    mes = _build_ops_card(
                        "❌ 告警阈值设置失败",
                        summary="阈值必须是大于等于 1 的整数。",
                        action="请执行 `warn <次数>` 或 `wlc <次数>`。",
                    )
            else:
                mes = _build_ops_card(
                    "📌 当前连输告警阈值",
                    summary="这是当前账号触发连输告警的阈值。",
                    fields=[("当前阈值", f"{rt.get('warning_lose_count', 3)} 次")],
                    action="如需调整，请执行 `warn <次数>` 或 `wlc <次数>`。",
                )
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return
        
        # model - 模型管理 - 使用与master一致的handle_model_command
        if cmd == "model":
            await handle_model_command_multiuser(event, my[1:], user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd in ("apikey", "ak"):
            await handle_apikey_command_multiuser(event, my[1:], user_ctx)
            # 防止 key 在命令消息中长期可见
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            return

        # ========== 发布更新命令 ==========
        if cmd in ("ver", "version"):
            result = await asyncio.to_thread(list_version_catalog, None, 3)
            if not result.get("success"):
                mes = _build_ops_card(
                    "❌ 版本查询失败",
                    summary="当前无法读取版本信息。",
                    fields=[("错误", result.get('error', 'unknown'))],
                    action="建议稍后重试；若持续失败，可先检查仓库状态或网络。",
                )
            else:
                mes = _build_version_catalog_message(result)

            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return

        if cmd in ("update", "up", "upnow", "upref", "upcommit"):
            target_ref = my[1].strip() if len(my) > 1 else ""
            await send_to_admin(
                client,
                _build_ops_card(
                    "🔄 开始更新",
                    summary="系统已经开始拉取并切换到目标版本。",
                    fields=[("目标", target_ref or "latest")],
                    action="请等待结果通知，更新完成后再执行 `restart`。",
                ),
                user_ctx,
                global_config,
            )
            result = await asyncio.to_thread(update_to_version, None, target_ref)
            if result.get("success"):
                if result.get("no_change"):
                    await send_to_admin(
                        client,
                        _build_ops_card(
                            "✅ 无需更新",
                            summary=result.get('message', '当前已是目标版本'),
                            action="如需确认当前状态，可执行 `ver` 或 `status`。",
                        ),
                        user_ctx,
                        global_config,
                    )
                else:
                    after = result.get("after", {})
                    resolved = result.get("resolved_target", "") or result.get("target_ref", target_ref or "latest")
                    mes = _build_ops_card(
                        "✅ 更新成功",
                        summary="代码已经切换到目标版本，但需要重启后才会实际生效。",
                        fields=[
                            ("目标", resolved),
                            ("当前", after.get('display_version', after.get('short_commit', 'unknown'))),
                        ],
                        action="请执行 `restart` 让新版本正式生效。",
                    )
                    await send_to_admin(client, mes, user_ctx, global_config)
            else:
                blocking_paths = result.get("blocking_paths", [])
                detail = result.get("detail", "")
                blocking_text = " / ".join(blocking_paths[:5]) if blocking_paths else ""
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 更新失败",
                        summary="本次更新没有完成，当前版本保持不变。",
                        fields=[
                            ("错误", result.get('error', 'unknown')),
                            ("阻塞文件", blocking_text),
                        ],
                        action="建议先处理阻塞文件，再重新执行 `update`。",
                        note=detail[:200] if detail else "",
                    ),
                    user_ctx,
                    global_config,
                )
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd in ("reback", "rollback", "uprollback"):
            target_ref = my[1].strip() if len(my) > 1 else ""
            if not target_ref:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 缺少回退目标",
                        summary="当前没有提供要回退到的版本、提交或分支。",
                        action="请执行 `reback <版本号|commit|branch>`。",
                    ),
                    user_ctx,
                    global_config,
                )
                return

            await send_to_admin(
                client,
                _build_ops_card(
                    "↩️ 开始回退",
                    summary="系统已经开始切换到指定历史版本。",
                    fields=[("目标", target_ref)],
                    action="请等待结果通知，回退完成后再执行 `restart`。",
                ),
                user_ctx,
                global_config,
            )
            result = await asyncio.to_thread(reback_to_version, None, target_ref)
            if result.get("success"):
                after = result.get("after", {})
                resolved = result.get("resolved_target", target_ref)
                mes = _build_ops_card(
                    "✅ 回退成功",
                    summary="代码已经切换到目标历史版本，但需要重启后才会正式生效。",
                    fields=[
                        ("目标", resolved),
                        ("当前", after.get('display_version', after.get('short_commit', 'unknown'))),
                    ],
                    action="请执行 `restart` 让回退版本正式生效。",
                )
                await send_to_admin(client, mes, user_ctx, global_config)
            else:
                mes = _build_ops_card(
                    "❌ 回退失败",
                    summary="本次回退没有完成，当前版本保持不变。",
                    fields=[("错误", result.get('error', 'unknown'))],
                    action="请确认目标版本或提交是否正确，再重新执行 `reback`。",
                    note=str(result.get('detail'))[:200] if result.get("detail") else "",
                )
                await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd in ("restart", "reboot"):
            service_name = resolve_systemd_service_name()
            if service_name:
                mes = _build_ops_card(
                    "♻️ 已接收重启指令",
                    summary="系统会在 2 秒后通过 systemd 重启服务。",
                    fields=[("服务名", service_name)],
                    action="重启期间消息可能短暂中断，建议稍后执行 `status` 确认恢复情况。",
                )
            else:
                mes = _build_ops_card(
                    "♻️ 已接收重启指令",
                    summary="系统会在 2 秒后自动重启当前进程。",
                    action="重启期间消息可能短暂中断，建议稍后执行 `status` 确认恢复情况。",
                )
            await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            asyncio.create_task(restart_process())
            return
        
        # ========== 数据管理命令 ==========
        # res - 重置命令 - 与master一致
        if cmd == "res":
            if len(my) > 1:
                if my[1] == "tj":
                    # 重置统计
                    rt["win_total"] = 0
                    rt["total"] = 0
                    rt["stats_last_report_total"] = 0
                    rt["earnings"] = 0
                    rt["period_profit"] = 0
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 统计数据已重置",
                        summary="收益、胜率和计数类统计已经清空。",
                        action="建议执行 `status` 查看当前状态是否符合预期。",
                    )
                    log_event(logging.INFO, 'user_cmd', '重置统计数据', user_id=user_ctx.user_id, action='completed')
                elif my[1] == "state":
                    # 重置状态
                    state.history = []
                    state.bet_type_history = []
                    rt["win_total"] = 0
                    rt["total"] = 0
                    rt["stats_last_report_total"] = 0
                    rt["earnings"] = 0
                    rt["period_profit"] = 0
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 状态文件已重置",
                        summary="历史、统计和运行态已清空到初始状态。",
                        action="如需重新开始，建议先执行 `st <预设名>`。",
                    )
                    log_event(logging.INFO, 'user_cmd', '重置状态文件', user_id=user_ctx.user_id, action='completed')
                elif my[1] == "bet":
                    # 重置押注策略
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["bet_reset_log_index"] = len(state.bet_sequence_log)
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["bet"] = False
                    rt["bet_on"] = False
                    rt["stop_count"] = 0
                    rt["flag"] = True
                    rt["mode_stop"] = True
                    rt["manual_pause"] = False
                    rt["pause_count"] = 0
                    rt["current_bet_seq"] = 1
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 押注策略已重置",
                        summary="当前连押链路已清空，后续会按首注重新开始。",
                        fields=[("初始金额", rt.get('initial_amount', 500))],
                        action="建议执行 `status` 确认当前状态，再等待下一次盘口。",
                    )
                    log_event(logging.INFO, 'user_cmd', '重置押注策略', user_id=user_ctx.user_id, action='completed')
                else:
                    mes = _build_ops_card(
                        "❌ 重置命令无效",
                        summary="当前重置类型无法识别。",
                        action="可用命令：`res tj`、`res state`、`res bet`。",
                    )
                    log_event(logging.WARNING, 'user_cmd', '无效重置命令', user_id=user_ctx.user_id, cmd=text)
            else:
                mes = _build_ops_card(
                    "📌 请选择重置类型",
                    summary="当前没有指定具体要重置的内容。",
                    action="请执行 `res tj`、`res state` 或 `res bet`。",
                )
            
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return
        
        # explain - 查看最近一次模型判断依据
        if cmd == "explain":
            last_logic_audit = rt.get("last_logic_audit", "")
            if last_logic_audit:
                log_event(logging.INFO, 'user_cmd', '查看决策解释', user_id=user_ctx.user_id)
                mes = f"🧠 **最近一次模型判断依据**\n```json\n{last_logic_audit}\n```"
            else:
                mes = "当前还没有可展示的模型判断记录，请等下一次有效判断后再查看。"
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return

        # balance - 查询余额 - 与master一致
        if cmd == "balance":
            try:
                balance = await fetch_balance(user_ctx)
                rt["account_balance"] = balance
                user_ctx.save_state()
                mes = _build_ops_card(
                    "💰 账户余额查询成功",
                    summary="余额已刷新到当前最新值。",
                    fields=[
                        ("账户余额", format_number(balance)),
                        ("菠菜资金", format_number(rt.get("gambling_fund", 0))),
                    ],
                    action="如需继续操作，建议再执行 `status` 查看完整概览。",
                )
                await send_to_admin(client, mes, user_ctx, global_config)
                log_event(logging.INFO, 'user_cmd', '查询余额', user_id=user_ctx.user_id, balance=balance)
            except Exception as e:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 账户余额查询失败",
                        summary="本次没有成功获取最新余额。",
                        fields=[("错误", str(e)[:180])],
                        action="建议稍后重试；若持续失败，请检查 Cookie 或网络状态。",
                    ),
                    user_ctx,
                    global_config,
                )
            return
        
        # ========== 预设管理命令 ==========
        # ys - 保存预设 - 与master一致
        if cmd == "ys" and len(my) >= 9:
            try:
                preset_name = my[1]
                ys = [int(my[2]), int(my[3]), float(my[4]), float(my[5]), float(my[6]), float(my[7]), int(my[8])]
                presets[preset_name] = ys
                user_ctx.save_presets()
                rt["current_preset_name"] = preset_name
                user_ctx.save_state()
                mes = _build_ops_card(
                    f"✅ 预设保存成功: {preset_name}",
                    summary="新的预设参数已经写入当前账号，并设置为当前预设。",
                    fields=[("策略参数", f"{ys[0]} {ys[1]} {ys[2]} {ys[3]} {ys[4]} {ys[5]} {ys[6]}")],
                    action=f"建议执行 `st {preset_name}` 或 `status` 确认当前状态。",
                )
                log_event(logging.INFO, 'user_cmd', '保存预设策略', user_id=user_ctx.user_id, preset=preset_name, params=ys)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            except (ValueError, IndexError) as e:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 预设保存失败",
                        summary="参数格式不正确，当前预设没有写入。",
                        fields=[("错误", str(e)[:180])],
                        action="请按 `ys [名] ...` 的格式重新输入完整参数。",
                    ),
                    user_ctx,
                    global_config,
                )
            return
        if cmd == "ys":
            await send_to_admin(
                client,
                _build_ops_card(
                    "❌ 预设保存失败",
                    summary="当前参数数量不足，预设没有写入。",
                    action="请按 `ys [名] [连续] [停] [倍1] [倍2] [倍3] [倍4] [首注]` 重新输入。",
                ),
                user_ctx,
                global_config,
            )
            return
        
        # yss - 查看/删除预设 - 与master一致
        if cmd == "yss":
            if len(my) > 2 and my[1] == "dl":
                # 删除预设
                preset_name = my[2]
                if preset_name in presets:
                    del presets[preset_name]
                    user_ctx.save_presets()
                    mes = _build_ops_card(
                        f"✅ 预设删除成功: {preset_name}",
                        summary="该预设已经从当前账号配置中移除。",
                        action="建议执行 `yss` 再确认剩余预设。",
                    )
                    log_event(logging.INFO, 'user_cmd', '删除预设', user_id=user_ctx.user_id, preset=preset_name)
                else:
                    mes = _build_ops_card(
                        "❌ 预设删除失败",
                        summary="目标预设不存在或命令格式不正确。",
                        action="请先执行 `yss` 查看当前预设名称，再执行 `yss dl [名]`。",
                    )
                    log_event(logging.WARNING, 'user_cmd', '删除预设失败', user_id=user_ctx.user_id, cmd=text)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            else:
                # 查看所有预设
                if len(presets) > 0:
                    max_key_length = max(len(str(k)) for k in presets.keys())
                    mes = _build_ops_card(
                        "📚 当前预设列表",
                        summary="以下是当前账号可用的全部预设。",
                        fields=[("预设", "\n".join(f"'{k.ljust(max_key_length)}': {v}" for k, v in presets.items()))],
                        action="删除可执行 `yss dl [名]`，启动可执行 `st [名]`。",
                    )
                    log_event(logging.INFO, 'user_cmd', '查看预设', user_id=user_ctx.user_id)
                else:
                    mes = _build_ops_card(
                        "📚 当前暂无预设",
                        summary="当前账号还没有保存任何自定义预设。",
                        action="可执行 `ys [名] ...` 新建预设，或直接使用内置预设。",
                    )
                    log_event(logging.INFO, 'user_cmd', '暂无预设', user_id=user_ctx.user_id)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 60))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        # ========== 测算命令 ==========
        if cmd == "yc":
            # 测算命令 - 与master一致
            await yc_command_handler_multiuser(client, event, my[1:], user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return
        
        # ms - 切换模式 - 与master一致
        if cmd == "ms":
            if len(my) > 1:
                try:
                    mode = int(my[1])
                    mode_names = {0: "反投", 1: "预测", 2: "追投"}
                    if mode in mode_names:
                        rt["bet_mode"] = mode
                        user_ctx.save_state()
                        mes = _build_ops_card(
                            "✅ 模式切换成功",
                            summary="后续会按新的下注模式继续运行。",
                            fields=[("当前模式", f"{mode_names[mode]} ({mode})")],
                            action="建议执行 `status` 确认当前状态。",
                        )
                        log_event(logging.INFO, 'user_cmd', '切换模式', user_id=user_ctx.user_id, mode=mode)
                    else:
                        mes = _build_ops_card(
                            "❌ 模式切换失败",
                            summary="当前模式值无效。",
                            action="可选模式：`0=反投`、`1=预测`、`2=追投`。",
                        )
                except ValueError:
                    mes = _build_ops_card(
                        "❌ 模式切换失败",
                        summary="模式参数必须是数字。",
                        action="请执行 `ms 0`、`ms 1` 或 `ms 2`。",
                    )
            else:
                current_mode = rt.get("bet_mode", 1)
                mode_names = {0: "反投", 1: "预测", 2: "追投"}
                mes = _build_ops_card(
                    "📌 当前下注模式",
                    summary="这是当前账号使用的下注模式。",
                    fields=[("当前模式", f"{mode_names.get(current_mode, '未知')} ({current_mode})")],
                    action="如需切换，请执行 `ms [0|1|2]`。",
                )
            
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return
        
        # ========== 多用户管理命令 ==========
        # users - 查看所有用户
        if cmd == "users":
            # 获取当前用户信息
            user_info = _build_ops_card(
                "👤 当前用户信息",
                summary="以下是当前账号的核心运行信息。",
                fields=[
                    ("账号", f"{user_ctx.config.name} (ID: {user_ctx.user_id})"),
                    ("菠菜资金", format_number(rt.get('gambling_fund', 0))),
                    ("状态", get_bet_status_text(rt)),
                    ("预设", rt.get('current_preset_name', '无')),
                    ("模型", rt.get('current_model_id', 'default')),
                    ("胜率", f"{rt.get('win_total', 0)}/{rt.get('total', 0)}"),
                ],
                action="如果需要更完整的运行态，请执行 `status`。",
            )
            message = await send_to_admin(client, user_info, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 30))
            return
        
        # 未知命令
        log_event(logging.DEBUG, 'user_cmd', '未知命令', user_id=user_ctx.user_id, data=text[:50])
        message = await send_to_admin(
            client,
            _build_ops_card(
                "❓ 未知命令",
                summary=f"`{cmd}` 不是当前支持的命令。",
                action="请执行 `help` 查看可用命令列表。",
            ),
            user_ctx,
            global_config,
        )
        asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
        
    except Exception as e:
        log_event(logging.ERROR, 'user_cmd', '命令执行出错', user_id=user_ctx.user_id, error=str(e))
        await send_to_admin(
            client,
            _build_ops_card(
                "❌ 命令执行出错",
                summary="本次命令没有执行完成。",
                fields=[("错误", str(e)[:180])],
                action="建议稍后重试；若持续失败，可执行 `status` 确认当前状态。",
            ),
            user_ctx,
            global_config,
        )


async def check_bet_status(client, user_ctx: UserContext, global_config: dict):
    """检查押注状态 - 与master版本一致"""
    rt = user_ctx.state.runtime
    if rt.get("manual_pause", False):
        return
    next_bet_amount = calculate_bet_amount(rt)
    if next_bet_amount <= 0:
        rt["bet"] = False
        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        if not rt.get("limit_stop_notified", False):
            lose_stop = int(rt.get("lose_stop", 13))
            await send_to_admin(
                client,
                _build_ops_card(
                    "⚠️ 已达到预设连投上限",
                    summary="当前链路已经到达设定的最大连投次数，系统将保持暂停。",
                    fields=[("当前上限", f"{lose_stop} 手")],
                    action="如需继续，可切换预设、重置策略，或等待新一轮开始。",
                ),
                user_ctx,
                global_config,
            )
            rt["limit_stop_notified"] = True
        user_ctx.save_state()
        return

    rt["limit_stop_notified"] = False
    if is_fund_available(user_ctx, next_bet_amount) and not rt.get("bet", False) and rt.get("switch", True) and rt.get("stop_count", 0) == 0:
        await _clear_pause_countdown_notice(client, user_ctx)
        # 这里只恢复“可下注状态”，不应提前标记为“已下注”。
        # bet=True 只能在真实点击下注成功后设置，避免结算时序误判。
        rt["bet"] = False
        rt["bet_on"] = True
        rt["mode_stop"] = True
        rt["pause_count"] = 0
        rt["fund_pause_notified"] = False
        user_ctx.save_state()
        mes = (
            "✅ 资金条件已满足，恢复可下注状态\n"
            f"当前资金：{rt.get('gambling_fund', 0) / 10000:.2f} 万\n"
            f"接续倍投金额：{format_number(next_bet_amount)}\n"
            "说明：本提示仅表示“可下注”，实际下注仍以盘口事件触发为准"
        )
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            mes,
            ttl_seconds=120,
            attr_name="status_transition_message",
        )
    elif not is_fund_available(user_ctx, next_bet_amount):
        if _sync_fund_from_account_when_insufficient(rt, next_bet_amount):
            log_event(
                logging.INFO,
                'status',
                '检查状态时资金不足触发资金同步',
                user_id=user_ctx.user_id,
                data=(
                    f"need={next_bet_amount}, fund={rt.get('gambling_fund', 0)}, "
                    f"account={rt.get('account_balance', 0)}"
                ),
            )
            user_ctx.save_state()

        if is_fund_available(user_ctx, next_bet_amount):
            await _clear_pause_countdown_notice(client, user_ctx)
            rt["bet"] = False
            rt["bet_on"] = True
            rt["mode_stop"] = True
            rt["pause_count"] = 0
            rt["fund_pause_notified"] = False
            user_ctx.save_state()
            mes = (
                "✅ 资金同步后已恢复可下注状态\n"
                f"当前资金：{rt.get('gambling_fund', 0) / 10000:.2f} 万\n"
                f"接续倍投金额：{format_number(next_bet_amount)}\n"
                "说明：本提示仅表示“可下注”，实际下注仍以盘口事件触发为准"
            )
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                mes,
                ttl_seconds=120,
                attr_name="status_transition_message",
            )
            return

        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        if not rt.get("fund_pause_notified", False):
            mes = "⚠️ 菠菜资金不足，已自动暂停押注"
            await send_message_v2(
                client,
                "fund_pause",
                mes,
                user_ctx,
                global_config,
                title=f"菠菜机器人 {user_ctx.config.name} 资金风控暂停",
                desp=mes,
            )
            rt["fund_pause_notified"] = True
        user_ctx.save_state()


def _parse_yc_params(args, presets):
    if not args:
        return None, None, (
            "📊 **测算功能**\n\n"
            "用法:\n"
            "`yc [预设名]` - 测算已有预设\n"
            "`yc [参数...]` - 自定义参数测算\n\n"
            "例: `yc yc05` 或 `yc 1 13 3 2.1 2.1 2.05 500`"
        )

    if args[0] in presets:
        preset = presets[args[0]]
        params = {
            "continuous": int(preset[0]),
            "lose_stop": int(preset[1]),
            "lose_once": float(preset[2]),
            "lose_twice": float(preset[3]),
            "lose_three": float(preset[4]),
            "lose_four": float(preset[5]),
            "initial_amount": int(preset[6]),
        }
        return params, args[0], None

    if len(args) >= 7:
        try:
            params = {
                "continuous": int(args[0]),
                "lose_stop": int(args[1]),
                "lose_once": float(args[2]),
                "lose_twice": float(args[3]),
                "lose_three": float(args[4]),
                "lose_four": float(args[5]),
                "initial_amount": int(args[6]),
            }
            return params, "自定义", None
        except ValueError:
            return None, None, "❌ 参数格式错误，请确保所有参数都是数字"

    return None, None, f"❌ 预设 `{args[0]}` 不存在，且参数不足7个"


def _calculate_yc_sequence(params):
    initial = max(0, int(params["initial_amount"]))
    lose_stop = max(1, int(params["lose_stop"]))
    table_steps = 15
    multipliers = [
        float(params["lose_once"]),
        float(params["lose_twice"]),
        float(params["lose_three"]),
        float(params["lose_four"]),
    ]
    max_single_bet_limit = 50_000_000
    start_streak = max(1, int(params["continuous"]))

    rows = []
    prev_bet = initial
    cumulative_loss = 0

    for i in range(table_steps):
        if i == 0:
            multiplier = 1.0
            bet = initial
        else:
            multiplier = multipliers[min(i - 1, 3)]
            bet = int(prev_bet * multiplier)

        if bet > max_single_bet_limit:
            bet = max_single_bet_limit

        cumulative_loss += bet
        profit_if_win = bet - (cumulative_loss - bet)
        rows.append(
            {
                "streak": start_streak + i,
                "multiplier": multiplier,
                "bet": bet,
                "profit_if_win": profit_if_win,
                "cumulative_loss": cumulative_loss,
            }
        )
        prev_bet = bet

    total_investment = rows[-1]["cumulative_loss"] if rows else 0
    max_bet = max((row["bet"] for row in rows), default=0)
    effective_rows = rows[:lose_stop]
    effective_streak = effective_rows[-1]["streak"] if effective_rows else start_streak
    effective_investment = effective_rows[-1]["cumulative_loss"] if effective_rows else 0
    effective_profit = effective_rows[-1]["profit_if_win"] if effective_rows else 0
    return {
        "rows": rows,
        "total_investment": total_investment,
        "max_bet": max_bet,
        "max_single_bet_limit": max_single_bet_limit,
        "start_streak": start_streak,
        "lose_stop": lose_stop,
        "table_steps": table_steps,
        "effective_rows": effective_rows,
        "effective_streak": effective_streak,
        "effective_investment": effective_investment,
        "effective_profit": effective_profit,
    }


def _build_yc_result_message(params, preset_name: str, current_fund: int, auto_trigger: bool) -> str:
    calc = _calculate_yc_sequence(params)
    rows = calc["rows"]
    effective_rows = calc["effective_rows"]
    effective_streak = calc["effective_streak"]
    effective_investment = calc["effective_investment"]
    effective_profit = calc["effective_profit"]
    max_single_bet_limit = calc["max_single_bet_limit"]

    def fmt_wan(value: int) -> str:
        return f"{value / 10000:,.1f}"

    def fmt_table_wan(value: int) -> str:
        wan = value / 10000
        if abs(wan) >= 1000:
            return f"{wan:,.0f}"
        return f"{wan:.1f}"

    header_line = "🔮 已根据当前预设自动测算\n" if auto_trigger else ""
    command_text = (
        f"{params['continuous']} {params['lose_stop']} "
        f"{params['lose_once']} {params['lose_twice']} {params['lose_three']} {params['lose_four']} {params['initial_amount']}"
    )

    fund_text = f"{fmt_wan(current_fund)}万" if current_fund > 0 else "未设置"
    cover_streak = 0
    cover_required = 0
    cover_profit = 0
    if current_fund > 0 and effective_rows:
        cover_rows = [row for row in effective_rows if row["cumulative_loss"] <= current_fund]
        if cover_rows:
            cover_row = cover_rows[-1]
            cover_streak = int(cover_row["streak"])
            cover_required = int(cover_row["cumulative_loss"])
            cover_profit = int(cover_row["profit_if_win"])
    elif effective_rows:
        cover_streak = int(effective_streak)
        cover_required = int(effective_investment)
        cover_profit = int(effective_profit)

    lines = []
    if header_line:
        lines.append(header_line.rstrip("\n"))
    lines.append("```")
    lines.extend(
        [
            "🎯 策略参数",
            f"预设名称：{preset_name}",
            f"菠菜资金：{fund_text}",
            f"策略命令: {command_text}",
            f"🏁 起始连数: {params['continuous']}",
            f"🔢 下注次数: {params['lose_stop']}次",
            f"💰 首注金额: {fmt_wan(int(params['initial_amount']))}万",
            f"💰 单注上限: {max_single_bet_limit / 10000:,.0f}万",
            "",
            "🎯 策略总结:",
            f"菠菜资金：{fund_text}",
            f"资金最多连数: {cover_streak}连",
            f"{cover_streak}连所需本金: {fmt_wan(cover_required)}万",
            f"{cover_streak}连获得盈利: {fmt_wan(cover_profit)}万",
            "",
            "连数|倍率|下注| 盈利 |所需本金",
            "---|----|------|------|------",
        ]
    )

    for row in rows:
        multiplier_text = f"{row['multiplier']:.2f}".rstrip("0")
        if multiplier_text.endswith("."):
            multiplier_text += "0"
        row_text = (
            f"{str(row['streak']).center(3)}|"
            f"{multiplier_text.center(4)}|"
            f"{fmt_table_wan(row['bet']).center(6)}|"
            f"{fmt_table_wan(row['profit_if_win']).center(6)}|"
            f"{fmt_table_wan(row['cumulative_loss']).center(6)}"
        )
        lines.append(row_text)

    lines.append("```")
    return "\n".join(lines)


async def yc_command_handler_multiuser(
    client,
    event,
    args,
    user_ctx: UserContext,
    global_config: dict,
    auto_trigger: bool = False,
):
    """处理 yc 测算命令，支持 st 切换预设后自动触发。"""
    presets = user_ctx.presets
    rt = user_ctx.state.runtime

    params, preset_name, error_msg = _parse_yc_params(args, presets)
    if error_msg:
        await send_to_admin(
            client,
            _build_ops_card(
                "❌ 测算命令无法执行",
                summary="当前测算参数不完整或格式不正确。",
                note=error_msg,
                action="请执行 `yc [预设名]` 或 `yc [参数...]`，例如 `yc yc05`。",
            ),
            user_ctx,
            global_config,
        )
        return

    result_msg = _build_yc_result_message(
        params=params,
        preset_name=preset_name,
        current_fund=int(rt.get("gambling_fund", 0)),
        auto_trigger=auto_trigger,
    )
    await send_to_admin(client, result_msg, user_ctx, global_config)
    log_event(
        logging.INFO,
        'yc',
        '测算完成',
        user_id=user_ctx.user_id,
        preset=preset_name,
        auto_trigger=auto_trigger,
    )


async def fetch_balance(user_ctx: UserContext) -> int:
    zhuque = user_ctx.config.zhuque
    cookie = zhuque.get("cookie", "")
    csrf_token = zhuque.get("csrf_token", "") or zhuque.get("x_csrf", "")
    api_url = zhuque.get("api_url", "https://zhuque.in/api/user/getInfo?")
    
    if not cookie or not csrf_token:
        return 0
    
    headers = {
        "Cookie": cookie,
        "X-Csrf-Token": csrf_token,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 401:
                    user_ctx.set_runtime("balance_status", "auth_failed")
                    log_event(logging.ERROR, 'balance', '认证失败(401)，请更新 Cookie',
                              user_id=user_ctx.user_id)
                    return user_ctx.get_runtime("account_balance", 0)
                
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict) and data.get("status", 200) != 200:
                        log_event(logging.WARNING, 'balance', 'API返回错误',
                                  user_id=user_ctx.user_id, message=data.get("message"))
                        return user_ctx.get_runtime("account_balance", 0)
                    
                    balance = int(data.get("data", {}).get("bonus", 0))
                    user_ctx.set_runtime("balance_status", "success")
                    return balance
    except Exception as e:
        user_ctx.set_runtime("balance_status", "network_error")
        log_event(logging.ERROR, 'balance', '获取余额失败',
                  user_id=user_ctx.user_id, data=str(e))
    
    return 0
