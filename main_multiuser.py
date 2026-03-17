"""
main_multiuser.py - 多用户版本主程序
版本: 2.0.0
日期: 2026-02-20
功能: 支持多用户并发运行的Telegram客户端
"""

import logging
import asyncio
import os
import time
import sys
import errno
import fcntl
from typing import Any, Dict, List
from telethon import TelegramClient, events
from logging.handlers import TimedRotatingFileHandler
from user_manager import UserManager, UserContext
from update_manager import periodic_release_check_loop

# 日志配置
logger = logging.getLogger('main_multiuser')
logger.setLevel(logging.DEBUG)
logger.propagate = False

_MAIN_ACCOUNT_NAME_REGISTRY: Dict[str, str] = {}
MAIN_ACCOUNT_LOG_ROOT = os.path.join("logs", "accounts")


def _sanitize_account_slug(text: str, fallback: str = "unknown") -> str:
    raw = str(text or "").strip().lower().replace(" ", "-")
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
    return cleaned or fallback


def _build_account_label(account_slug: str) -> str:
    return f"ydx-{account_slug}"


def register_main_user_log_identity(user_ctx: UserContext) -> str:
    user_id = str(getattr(user_ctx, "user_id", 0) or 0)
    account_name = str(getattr(getattr(user_ctx, "config", None), "name", "") or "").strip()
    if not account_name:
        account_name = f"user-{user_id}"
    _MAIN_ACCOUNT_NAME_REGISTRY[user_id] = account_name
    return account_name


def _infer_main_log_category(level: int, module: str, event: str) -> str:
    if level >= logging.WARNING:
        return "warning"
    text = f"{module}:{event}".lower()
    if any(token in text for token in ("start", "login", "release", "check", "health")):
        return "runtime"
    return "business"


class _MainLogDefaultsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "custom_module"):
            record.custom_module = "main"
        if not hasattr(record, "event"):
            record.event = "general"
        if not hasattr(record, "data"):
            record.data = ""
        if not hasattr(record, "user_id"):
            record.user_id = "0"
        if not hasattr(record, "category"):
            record.category = _infer_main_log_category(record.levelno, str(record.custom_module), str(record.event))
        if not hasattr(record, "account_slug"):
            fallback_slug = f"user-{record.user_id}" if str(record.user_id) != "0" else "unknown"
            record.account_slug = _sanitize_account_slug("", fallback=fallback_slug)
        if not hasattr(record, "account_tag"):
            record.account_tag = f"【ydx-{record.account_slug}】"
        return True


_main_log_filter = _MainLogDefaultsFilter()

file_handler = TimedRotatingFileHandler('numai.log', when='midnight', interval=1, backupCount=3, encoding='utf-8')
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] [%(custom_module)s:%(event)s] %(message)s | %(data)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
file_handler.setLevel(logging.DEBUG)
file_handler.addFilter(_main_log_filter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] %(message)s | %(data)s',
    datefmt='%H:%M:%S'
))
console_handler.setLevel(logging.INFO)
console_handler.addFilter(_main_log_filter)
logger.addHandler(console_handler)


class _MainAccountCategoryRouterHandler(logging.Handler):
    def __init__(self, root_dir: str, backup_count: int = 7):
        super().__init__(level=logging.DEBUG)
        self.root_dir = root_dir
        self.backup_count = backup_count
        self._handlers: Dict[tuple, TimedRotatingFileHandler] = {}
        self._default_filter = _MainLogDefaultsFilter()
        self._formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] [%(custom_module)s:%(event)s] %(message)s | %(data)s',
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
            if account_slug == "unknown":
                return
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


class _MainAccountIdentityFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        account_slug = str(getattr(record, "account_slug", "") or "").strip()
        if not account_slug:
            fallback_slug = f"user-{getattr(record, 'user_id', '0')}" if str(getattr(record, "user_id", "0")) != "0" else "unknown"
            account_slug = _sanitize_account_slug("", fallback=fallback_slug)
            record.account_slug = account_slug
        record.account_label = _build_account_label(account_slug)
        record.account_tag = f"【ydx-{account_slug}】"
        return True


account_category_handler = _MainAccountCategoryRouterHandler(MAIN_ACCOUNT_LOG_ROOT, backup_count=7)
account_category_handler.addFilter(_main_log_filter)
_main_account_identity_filter = _MainAccountIdentityFilter()
account_category_handler.addFilter(_main_account_identity_filter)
logger.addHandler(account_category_handler)
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(account_label)s | %(message)s',
    datefmt='%H:%M:%S'
))
console_handler.addFilter(_main_account_identity_filter)
try:
    logger.removeHandler(file_handler)
    file_handler.close()
except Exception:
    pass


def log_event(level, module, event=None, message='', **kwargs):
    # 兼容3参数调用: log_event(level, module, event)
    if event is None:
        event = module
        module = 'main'
        message = ''
    elif not message and not kwargs:
        # log_event(level, module, event) - event作为message
        message = event
        event = module
        module = 'main'
    category = str(kwargs.pop("category", "")).strip().lower()
    account_name = str(kwargs.pop("account_name", "")).strip()
    user_id = str(kwargs.get("user_id", 0))
    if not account_name:
        account_name = _MAIN_ACCOUNT_NAME_REGISTRY.get(user_id, "")
    if not account_name and user_id not in {"", "0"}:
        account_name = f"user-{user_id}"
    account_slug = _sanitize_account_slug(account_name, fallback=(f"user-{user_id}" if user_id not in {"", "0"} else "unknown"))
    if category not in {"runtime", "warning", "business"}:
        category = _infer_main_log_category(level, str(module), str(event))
    data = ', '.join(f'{k}={v}' for k, v in kwargs.items())
    logger.log(
        level,
        message,
        extra={
            'custom_module': module,
            'event': event,
            'data': data,
            'user_id': user_id,
            'category': category,
            'account_slug': account_slug,
            'account_tag': f"【ydx-{account_slug}】",
        },
    )


async def create_client(user_ctx: UserContext, global_config: dict) -> TelegramClient:
    session_path = os.path.join(
        user_ctx.user_dir, 
        user_ctx.config.telegram.get("session_name", "session")
    )
    
    client = TelegramClient(
        session_path,
        user_ctx.config.telegram.get("api_id"),
        user_ctx.config.telegram.get("api_hash")
    )
    return client


def _get_session_path(user_ctx: UserContext) -> str:
    return os.path.join(
        user_ctx.user_dir,
        user_ctx.config.telegram.get("session_name", "session")
    )


def _acquire_session_lock(user_ctx: UserContext) -> bool:
    """
    为每个账号的 Telethon session 增加进程级文件锁，避免多个进程同时写同一个 .session。
    这类并发写入会触发 sqlite3.OperationalError: database is locked。
    """
    session_path = _get_session_path(user_ctx)
    lock_path = f"{session_path}.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        setattr(user_ctx, "_session_lock_fd", fd)
        setattr(user_ctx, "_session_lock_path", lock_path)
        return True
    except OSError as e:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if e.errno in (errno.EACCES, errno.EAGAIN):
            log_event(
                logging.ERROR,
                'start',
                '账号session已被其他进程占用',
                user_id=user_ctx.user_id,
                session=session_path,
                lock=lock_path,
            )
            return False
        raise


def _release_session_lock(user_ctx: UserContext):
    fd = getattr(user_ctx, "_session_lock_fd", None)
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass
    setattr(user_ctx, "_session_lock_fd", None)


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


def _normalize_target(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except Exception:
                return value
        return text
    return value


def _iter_targets(target: Any) -> List[Any]:
    if isinstance(target, (list, tuple, set)):
        result: List[Any] = []
        for item in target:
            normalized = _normalize_target(item)
            if normalized not in (None, ""):
                result.append(normalized)
        return result
    normalized = _normalize_target(target)
    if normalized in (None, ""):
        return []
    return [normalized]


def _get_user_event_lock(user_ctx: UserContext) -> asyncio.Lock:
    lock = getattr(user_ctx, "_event_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(user_ctx, "_event_lock", lock)
    return lock


def _normalize_ai_keys(ai_cfg: Any) -> List[str]:
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


def _looks_like_ai_key_issue(error_text: str) -> bool:
    text = str(error_text or "").lower()
    if not text:
        return False
    return any(sig in text for sig in ("401", "unauthorized", "authentication", "invalid api key", "invalid token", "forbidden"))


def _get_allowed_sender_ids(user_ctx: UserContext) -> set:
    """
    可选命令发送者白名单（默认关闭，保持兼容）。
    支持 notification.allowed_sender_ids / allowed_senders / admins。
    """
    notification = user_ctx.config.notification if isinstance(user_ctx.config.notification, dict) else {}
    raw = (
        notification.get("allowed_sender_ids")
        or notification.get("allowed_senders")
        or notification.get("admins")
    )
    if not raw:
        return set()

    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    result = set()
    for item in items:
        normalized = _normalize_target(item)
        if normalized in (None, ""):
            continue
        result.add(str(normalized))
    return result


def register_handlers(client: TelegramClient, user_ctx: UserContext, global_config: dict):
    config = user_ctx.config
    state = user_ctx.state
    presets = user_ctx.presets
    button_mapping = global_config.get("button_mapping", {})
    admin_chat = _resolve_admin_chat(user_ctx)
    zq_group_targets = _iter_targets(config.groups.get("zq_group", []))
    zq_bot_targets = _iter_targets(config.groups.get("zq_bot"))
    
    @client.on(events.NewMessage(
        chats=zq_group_targets,
        pattern=r"\[近 40 次结果\]\[由近及远\]\[0 小 1 大\].*",
        from_users=zq_bot_targets
    ))
    async def bet_on_handler(event):
        log_event(logging.DEBUG, 'bet_on', '收到押注触发消息', 
                  user_id=user_ctx.user_id, msg_id=event.id)
        async with _get_user_event_lock(user_ctx):
            await zq_bet_on(client, event, user_ctx, global_config)
    
    @client.on(events.NewMessage(
        chats=zq_group_targets,
        # 修复：多用户分支 - 结算正则字符类误写会匹配到 `|`，导致异常消息也被当作结算。
        pattern=r"已结算: 结果为 (\d+) (大|小)",
        from_users=zq_bot_targets
    ))
    async def settle_handler(event):
        log_event(logging.DEBUG, 'settle', '收到结算消息',
                  user_id=user_ctx.user_id, msg_id=event.id)
        async with _get_user_event_lock(user_ctx):
            await zq_settle(client, event, user_ctx, global_config)

    @client.on(events.NewMessage(
        chats=zq_group_targets,
        from_users=zq_bot_targets
    ))
    async def red_packet_handler(event):
        await zq_red_packet(client, event, user_ctx, global_config)
    
    @client.on(events.NewMessage(chats=admin_chat if admin_chat else []))
    async def user_handler(event):
        raw_text = (event.raw_text or "").strip()
        safe_cmd = raw_text[:50]
        lower_text = raw_text.lower()
        if lower_text.startswith("apikey ") or lower_text.startswith("/apikey "):
            safe_cmd = "apikey ***"
        log_event(logging.DEBUG, 'user_cmd', '收到用户命令',
                  user_id=user_ctx.user_id, cmd=safe_cmd)
        allowed_senders = _get_allowed_sender_ids(user_ctx)
        if allowed_senders:
            sender_id = getattr(event, "sender_id", None)
            if sender_id is None or str(sender_id) not in allowed_senders:
                log_event(
                    logging.WARNING,
                    'user_cmd',
                    '命令发送者不在白名单，已忽略',
                    user_id=user_ctx.user_id,
                    sender_id=sender_id,
                )
                return
        async with _get_user_event_lock(user_ctx):
            await zq_user(client, event, user_ctx, global_config)


async def zq_bet_on(client, event, user_ctx: UserContext, global_config: dict):
    from zq_multiuser import process_bet_on
    await process_bet_on(client, event, user_ctx, global_config)


async def zq_settle(client, event, user_ctx: UserContext, global_config: dict):
    from zq_multiuser import process_settle
    await process_settle(client, event, user_ctx, global_config)


async def zq_user(client, event, user_ctx: UserContext, global_config: dict):
    from zq_multiuser import process_user_command
    await process_user_command(client, event, user_ctx, global_config)


async def zq_red_packet(client, event, user_ctx: UserContext, global_config: dict):
    from zq_multiuser import process_red_packet
    await process_red_packet(client, event, user_ctx, global_config)


async def check_models_for_user(client, user_ctx: UserContext):
    try:
        user_model_mgr = user_ctx.get_model_manager()
        user_model_mgr.load_models()
        models = user_model_mgr.list_models()
        
        report = f"🚀 **Bot 启动模型自检报告**\n\n"
        report += f"👤 **用户**: {user_ctx.config.name}\n\n"
        
        total_models = sum(len(ms) for ms in models.values())
        success_count = 0
        failure_errors: List[str] = []
        
        for provider, ms in models.items():
            report += f"📁 **{provider.upper()}**\n"
            for m in ms:
                mid = m['model_id']
                if not m.get('enabled', True):
                    report += f"⚪ `{mid}`: 已禁用\n"
                    continue
                
                res = await user_model_mgr.validate_model(mid)
                if res['success']:
                    status = "✅ 正常"
                    latency = res.get('latency', 'N/A')
                    success_count += 1
                else:
                    status = f"❌ 失败"
                    latency = "-"
                    failure_errors.append(str(res.get("error", "")))
                
                report += f"{status} `{mid}` ({latency}ms)\n"
            report += "\n"
        
        report += f"📊 **汇总**: {success_count}/{total_models} 可用\n"
        report += f"🤖 **当前默认**: `{user_ctx.get_runtime('current_model_id', 'qwen3-coder-plus')}`"
        
        admin_chat = _resolve_admin_chat(user_ctx)
        if admin_chat:
            await client.send_message(admin_chat, report)

            ai_cfg = user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {}
            has_keys = bool(_normalize_ai_keys(ai_cfg))
            key_issue_detected = (not has_keys) or any(_looks_like_ai_key_issue(err) for err in failure_errors)
            if key_issue_detected:
                warn = (
                    "⚠️ 大模型AI key 失效/缺失，请更新 key！！！\n"
                    "请在管理员窗口执行：`apikey set <新key>`"
                )
                await client.send_message(admin_chat, warn)
        log_event(logging.INFO, 'model_check', '模型自检完成', user_id=user_ctx.user_id)
        
    except Exception as e:
        log_event(logging.ERROR, 'model_check', '模型自检失败', 
                  user_id=user_ctx.user_id, error=str(e))


async def fetch_account_balance(user_ctx: UserContext) -> int:
    import aiohttp
    
    zhuque = user_ctx.config.zhuque
    cookie = zhuque.get("cookie", "")
    csrf_token = zhuque.get("csrf_token", "") or zhuque.get("x_csrf", "")
    api_url = zhuque.get("api_url", "https://zhuque.in/api/user/getInfo?")
    
    if not cookie or not csrf_token:
        log_event(logging.ERROR, 'balance', '缺少朱雀配置', user_id=user_ctx.user_id)
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
                    log_event(logging.INFO, 'balance', '获取余额成功',
                              user_id=user_ctx.user_id, balance=balance)
                    return balance
                else:
                    user_ctx.set_runtime("balance_status", "network_error")
                    log_event(logging.ERROR, 'balance', '获取余额失败',
                              user_id=user_ctx.user_id, status=response.status)
                    return user_ctx.get_runtime("account_balance", 0)
    except Exception as e:
        user_ctx.set_runtime("balance_status", "network_error")
        log_event(logging.ERROR, 'balance', '获取余额异常',
                  user_id=user_ctx.user_id, error=str(e))
        return user_ctx.get_runtime("account_balance", 0)


async def start_user(user_ctx: UserContext, global_config: dict):
    lock_acquired = False
    try:
        register_main_user_log_identity(user_ctx)
        try:
            from zq_multiuser import register_user_log_identity
            register_user_log_identity(user_ctx)
        except Exception as e:
            log_event(
                logging.WARNING,
                'start',
                '注册业务日志账号标识失败',
                user_id=user_ctx.user_id,
                error=str(e),
                category='warning',
            )

        zq_group_targets = _iter_targets(user_ctx.config.groups.get("zq_group", []))
        zq_bot_targets = _iter_targets(user_ctx.config.groups.get("zq_bot"))
        admin_chat = _resolve_admin_chat(user_ctx)

        # 启动前校验，避免“进程运行但账号无命令/无结算”的静默失败。
        if not zq_group_targets or not zq_bot_targets:
            log_event(
                logging.ERROR,
                'start',
                '用户启动失败：缺少必要监听配置',
                user_id=user_ctx.user_id,
                zq_group=zq_group_targets,
                zq_bot=zq_bot_targets,
            )
            return None
        if not admin_chat:
            log_event(
                logging.WARNING,
                'start',
                '未配置 admin_chat，命令与仪表盘将不可用',
                user_id=user_ctx.user_id,
            )

        lock_acquired = _acquire_session_lock(user_ctx)
        if not lock_acquired:
            return None

        client = await create_client(user_ctx, global_config)
        user_ctx.client = client
        
        await client.connect()
        
        if not await client.is_user_authorized():
            log_event(logging.WARNING, 'start', '用户未授权，开始登录流程',
                      user_id=user_ctx.user_id)
            if not sys.stdin.isatty():
                log_event(
                    logging.ERROR,
                    'start',
                    '非交互环境无法执行登录，请先在交互终端完成账号授权',
                    user_id=user_ctx.user_id,
                    session=user_ctx.config.telegram.get("session_name", ""),
                )
                _release_session_lock(user_ctx)
                return None
            print(f"\n🔐 用户 {user_ctx.config.name} 需要登录 Telegram")
            print(f"   请按照提示输入手机号和验证码...\n")
            try:
                await client.start()
                log_event(logging.INFO, 'start', '登录成功',
                          user_id=user_ctx.user_id)
                print(f"✅ 用户 {user_ctx.config.name} 登录成功！\n")
            except Exception as e:
                log_event(logging.ERROR, 'start', '登录失败',
                          user_id=user_ctx.user_id, error=str(e))
                print(f"❌ 登录失败: {e}")
                _release_session_lock(user_ctx)
                return None
        
        register_handlers(client, user_ctx, global_config)
        
        # await check_models_for_user(client, user_ctx)
        
        balance = await fetch_account_balance(user_ctx)
        # 复原为旧机制：启动只更新账户余额，不自动覆盖菠菜资金。
        user_ctx.set_runtime("account_balance", balance)
        log_event(
            logging.INFO,
            'start',
            '启动余额快照已刷新（菠菜资金保持独立）',
            user_id=user_ctx.user_id,
            account_balance=balance,
            gambling_fund=user_ctx.get_runtime("gambling_fund", 0),
        )

        # 启动恢复：按账号默认风控模式生效，并清理历史遗留挂单。
        from zq_multiuser import (
            apply_account_risk_default_mode,
            build_startup_focus_reminder,
            heal_stale_pending_bets,
        )
        risk_mode = apply_account_risk_default_mode(user_ctx.state.runtime)
        log_event(
            logging.INFO,
            'start',
            '应用账号默认风控模式',
            user_id=user_ctx.user_id,
            base=risk_mode.get("base_enabled"),
            deep=risk_mode.get("deep_enabled"),
        )
        heal_result = heal_stale_pending_bets(user_ctx)

        user_ctx.save_state()

        healed_count = int(heal_result.get("count", 0) or 0)
        if healed_count > 0:
            healed_preview = ", ".join(heal_result.get("items", [])[:5])
            if len(heal_result.get("items", [])) > 5:
                healed_preview += " ..."
            log_event(
                logging.WARNING,
                'start',
                '检测到历史未结算挂单并已自愈',
                user_id=user_ctx.user_id,
                count=healed_count,
                items=healed_preview,
            )
            if admin_chat:
                mes = (
                    "🩹 挂单自愈已执行\n"
                    f"检测到历史异常挂单：{healed_count} 笔（result=None）\n"
                    "处理方式：已自动标记为“异常未结算”（不再参与胜率/连输统计）\n"
                    f"样例：{healed_preview}"
                )
                try:
                    await client.send_message(admin_chat, mes)
                except Exception as e:
                    log_event(
                        logging.ERROR,
                        'start',
                        '挂单自愈通知发送失败',
                        user_id=user_ctx.user_id,
                        error=str(e),
                    )

        if admin_chat:
            pass
            # try:
            #     focus_msg = build_startup_focus_reminder(user_ctx)
            #     await client.send_message(admin_chat, focus_msg)
            # except Exception as e:
            #     log_event(
            #         logging.ERROR,
            #         'start',
            #         '启动重点设置提醒发送失败',
            #         user_id=user_ctx.user_id,
            #         error=str(e),
            #     )
        
        log_event(logging.INFO, 'start', '用户启动成功',
                  user_id=user_ctx.user_id, name=user_ctx.config.name, balance=balance)
        
        return client
        
    except Exception as e:
        log_event(logging.ERROR, 'start', '用户启动失败',
                  user_id=user_ctx.user_id, error=str(e))
        if lock_acquired:
            _release_session_lock(user_ctx)
        return None


async def main():
    print("=" * 50)
    print("多用户 Telegram Bot 启动中...")
    print("=" * 50)
    
    user_manager = UserManager()
    user_count = user_manager.load_all_users()
    
    if user_count == 0:
        print("❌ 未找到任何用户配置！")
        print("请在 users/ 目录下创建用户配置文件。")
        print("参考 users/_template/ 目录中的模板文件。")
        return
    
    print(f"✅ 已加载 {user_count} 个用户配置")
    log_event(logging.INFO, 'main', '加载用户配置', count=user_count)
    
    clients = []
    tasks = []
    
    for user_id, user_ctx in user_manager.get_all_users().items():
        print(f"🔄 正在启动用户: {user_ctx.config.name} (ID: {user_id})...")
        client = await start_user(user_ctx, user_manager.global_config)
        
        if client:
            clients.append(client)
            tasks.append(client.run_until_disconnected())
            print(f"✅ 用户 {user_ctx.config.name} 启动成功")
        else:
            print(f"❌ 用户 {user_ctx.config.name} 启动失败")
    
    if not clients:
        print("❌ 没有成功启动任何用户，程序退出")
        return
    
    print("=" * 50)
    print(f"🚀 所有用户已启动，共 {len(clients)} 个客户端运行中")
    print("=" * 50)
    log_event(logging.INFO, 'main', '所有用户启动完成', count=len(clients))

    async def notify_release(message: str):
        sent_admins = set()
        for user_ctx in user_manager.get_all_users().values():
            admin_chat = _resolve_admin_chat(user_ctx)
            if not admin_chat or admin_chat in sent_admins or not user_ctx.client:
                continue
            try:
                await user_ctx.client.send_message(admin_chat, message)
                sent_admins.add(admin_chat)
            except Exception as e:
                log_event(
                    logging.ERROR,
                    'release_check',
                    '发布通知发送失败',
                    user_id=user_ctx.user_id,
                    error=str(e),
                )

    asyncio.create_task(periodic_release_check_loop(notify_release))
    
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for user_ctx in user_manager.get_all_users().values():
            user_ctx.save_state()
            _release_session_lock(user_ctx)
    
    log_event(logging.INFO, 'main', '程序正常退出')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 脚本已手动终止")
        log_event(logging.INFO, 'main', 'stop', message='脚本被用户手动终止')
    except Exception as e:
        log_event(logging.ERROR, 'main', 'error', message='启动失败', error=str(e))
