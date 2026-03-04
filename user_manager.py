"""
user_manager.py - 多用户管理模块
版本: 2.4.3
日期: 2026-02-21
功能: 用户配置加载、状态管理、多用户隔离
"""

import os
import json
import threading
import logging
import importlib.util
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from logging.handlers import TimedRotatingFileHandler
import constants

# 日志配置
logger = logging.getLogger('user_manager')
logger.setLevel(logging.DEBUG)
file_handler = TimedRotatingFileHandler('user_manager.log', when='midnight', interval=1, backupCount=3, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)


def log_event(level, module, event, message=None, **kwargs):
    # 兼容旧调用: log_event(level, event, message, data)
    if message is None:
        message = event
        event = module
        module = 'user_manager'
    data = ', '.join(f'{k}={v}' for k, v in kwargs.items())
    logger.log(level, f"[{module}:{event}] {message} | {data}")


def load_json_with_comments(filepath: str) -> Dict[str, Any]:
    """
    读取支持注释的 JSON 文件。
    说明：
    - 支持整行注释：# ... 或 // ...
    - 支持行尾注释：... # ... 或 ... // ...
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        raw_text = f.read()

    cleaned_lines = []
    for raw_line in raw_text.splitlines():
        stripped = raw_line.lstrip()
        if stripped.startswith('#') or stripped.startswith('//'):
            continue

        in_string = False
        escaped = False
        cleaned = []
        i = 0
        while i < len(raw_line):
            ch = raw_line[i]
            if escaped:
                cleaned.append(ch)
                escaped = False
                i += 1
                continue
            if ch == '\\':
                cleaned.append(ch)
                escaped = True
                i += 1
                continue
            if ch == '"':
                in_string = not in_string
                cleaned.append(ch)
                i += 1
                continue
            if not in_string:
                if ch == '#':
                    break
                if ch == '/' and i + 1 < len(raw_line) and raw_line[i + 1] == '/':
                    break
            cleaned.append(ch)
            i += 1

        line = ''.join(cleaned).rstrip()
        if line:
            cleaned_lines.append(line)

    return json.loads('\n'.join(cleaned_lines))


def merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并字典: override 覆盖 base。"""
    result = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class UserConfig:
    user_id: int
    name: str
    telegram: Dict[str, Any] = field(default_factory=dict)
    groups: Dict[str, Any] = field(default_factory=dict)
    zhuque: Dict[str, Any] = field(default_factory=dict)
    notification: Dict[str, Any] = field(default_factory=dict)
    ai: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UserState:
    """
    用户状态数据类
    包含所有运行时变量，与 master 分支 variable.py 保持一致
    """
    # 历史数据
    history: List[int] = field(default_factory=list)
    bet_type_history: List[int] = field(default_factory=list)
    predictions: List[int] = field(default_factory=list)
    bet_sequence_log: List[Dict] = field(default_factory=list)
    
    # 运行时变量（存储在 runtime 字典中）
    runtime: Dict[str, Any] = field(default_factory=dict)
    
    def get_runtime(self, key: str, default: Any = None) -> Any:
        """获取运行时变量，支持类型转换"""
        value = self.runtime.get(key, default)
        # 如果默认值是 int，但存储的是字符串，进行转换
        if default is not None and isinstance(default, int) and isinstance(value, str):
            try:
                return int(value)
            except (ValueError, TypeError):
                return default
        if default is not None and isinstance(default, float) and isinstance(value, str):
            try:
                return float(value)
            except (ValueError, TypeError):
                return default
        return value
    
    def set_runtime(self, key: str, value: Any):
        """设置运行时变量"""
        self.runtime[key] = value


def get_default_runtime() -> Dict[str, Any]:
    """
    获取默认运行时变量
    与 master 分支 variable.py 保持一致
    """
    from datetime import datetime
    return {
        # 核心控制变量
        "switch": True,
        "open_ydx": False,
        "manual_pause": False,
        "consequence": "大",
        
        # 历史和状态管理
        "current_round": 1,
        "current_bet_seq": 1,
        "last_reset_date": datetime.now().strftime("%Y%m%d"),
        
        # 押注参数
        "bet": False,
        "bet_on": False,
        "mode": 1,
        "mode_stop": True,
        "bet_type": 0,
        "initial_amount": 500,
        "bet_amount": 0,
        "bet_sequence_count": 0,
        "lose_stop": 20,
        "warning_lose_count": 3,
        "lose_notify_pending": False,
        "lose_start_info": {},
        "last_logic_audit": "",
        "last_settle_message_id": 0,
        # 自动风控暂停周期状态
        "risk_pause_acc_rounds": 0,
        "risk_pause_snapshot_count": -1,
        "risk_pause_cycle_active": False,
        "risk_pause_recovery_passes": 0,
        "risk_base_hit_streak": 0,
        "risk_pause_level1_hit": False,
        "risk_deep_triggered_milestones": [],
        "risk_pause_priority_notified": False,
        "risk_pause_block_hits": 0,
        "risk_pause_block_rounds": 0,
        "risk_pause_last_100_report_total": 0,
        "lose_once": 2.5,
        "lose_twice": 2.5,
        "lose_three": 2.5,
        "lose_four": 2.1,
        "continuous": 10,
        "explode": 1,
        "stop": 3,
        "profit_stop": 2,
        "explode_count": 0,
        "stop_count": 0,
        "mark": True,
        "flag": True,
        "pause_count": 0,
        "pause_resume_pending": False,
        "pause_resume_pending_reason": "",
        "pause_countdown_active": False,
        "pause_countdown_reason": "",
        "pause_countdown_total_rounds": 0,
        "pause_countdown_last_remaining": -1,
        "entry_timeout_gate_last_seq": -1,
        "entry_timeout_gate_last_settled": -1,
        "stall_guard_sequence": -1,
        "stall_guard_last_history_len": -1,
        "stall_guard_no_bet_streak": 0,
        "stall_guard_skip_streak": 0,
        "stall_guard_timeout_streak": 0,
        "stall_guard_gate_streak": 0,
        "stall_guard_force_unlock_total": 0,
        "shadow_probe_active": False,
        "shadow_probe_origin_reason": "",
        "shadow_probe_target_rounds": 0,
        "shadow_probe_pass_required": 0,
        "shadow_probe_checked": 0,
        "shadow_probe_hits": 0,
        "shadow_probe_pending_prediction": None,
        "shadow_probe_last_history_len": -1,
        "shadow_probe_rearm": False,
        "last_skip_notice_settled": -1,
        "model_monitor_last_settled": 0,
        "fund_pause_notified": False,
        "limit_stop_notified": False,
        
        # 统计和仪表盘变量
        "total": 0,
        "stats_last_report_total": 0,
        "win_total": 0,
        "earnings": 0,
        "period_profit": 0,
        "profit": 1000000,
        "win_count": 0,
        "lose_count": 0,
        "gambling_fund": 25000000,
        "account_balance": 0,
        "balance_status": "unknown",
        "status": 0,
        
        # 算法相关变量
        "last_predict_info": "V10 预测",
        "api_key_index": 0,
        "current_model_id": "qwen3-coder-plus",
        
        # 预设相关
        "current_preset_name": "",
    }


class UserContext:
    def __init__(self, user_dir: str, global_config: Optional[Dict[str, Any]] = None):
        self.user_dir = user_dir
        self.global_config = global_config or {}
        self.user_id = 0  # 临时值，将在加载配置后更新
        self.config: Optional[UserConfig] = None
        self.state: Optional[UserState] = None
        self.presets: Dict[str, List] = {}
        self.client = None
        self._model_manager = None
        self._model_manager_ai_sig = ""
        self._config_path = ""
        self._config_data = {}
        self._lock = threading.Lock()
        self._load_all()
    
    def _load_all(self):
        self._load_config()
        self._load_state()
        self._load_presets()

    def _resolve_user_config_path(self) -> str:
        """
        优先读取 <账号目录名>_config.json；
        兼容旧版 config.json（便于平滑迁移）。
        """
        dir_name = os.path.basename(os.path.normpath(self.user_dir))
        preferred = os.path.join(self.user_dir, f"{dir_name}_config.json")
        legacy = os.path.join(self.user_dir, "config.json")

        if os.path.exists(preferred):
            return preferred

        # 兜底：若目录下仅存在一个 *_config.json，也可直接使用。
        try:
            matches = [
                os.path.join(self.user_dir, name)
                for name in os.listdir(self.user_dir)
                if name.endswith("_config.json")
            ]
            if len(matches) == 1 and os.path.exists(matches[0]):
                return matches[0]
        except Exception:
            pass

        if os.path.exists(legacy):
            return legacy

        raise FileNotFoundError(
            f"配置文件不存在，期望: {preferred} 或 {legacy}"
        )
    
    def _load_config(self):
        config_path = self._resolve_user_config_path()

        data = load_json_with_comments(config_path)
        self._config_path = config_path
        self._config_data = dict(data) if isinstance(data, dict) else {}
        global_cfg = self.global_config or {}

        account_cfg = merge_dict(global_cfg.get("account", {}), data.get("account", {}))
        telegram_cfg = merge_dict(global_cfg.get("telegram", {}), data.get("telegram", {}))
        groups_cfg = merge_dict(global_cfg.get("groups", {}), data.get("groups", {}))
        zhuque_cfg = merge_dict(global_cfg.get("zhuque", {}), data.get("zhuque", {}))
        # 精简规则：通知和 AI 仅从账号配置读取（不走全局继承）
        notification_cfg = data.get("notification", {}) if isinstance(data.get("notification", {}), dict) else {}
        ai_cfg = data.get("ai", {}) if isinstance(data.get("ai", {}), dict) else {}

        # 规范化：admin_chat 统一归类到 notification；兼容旧 groups.admin_chat 读取。
        admin_chat = notification_cfg.get("admin_chat")
        if admin_chat in (None, ""):
            admin_chat = groups_cfg.get("admin_chat")
        if admin_chat not in (None, ""):
            notification_cfg["admin_chat"] = admin_chat
            groups_cfg["admin_chat"] = admin_chat
        
        # 从配置中读取user_id，如果没有则使用目录名的哈希值
        self.user_id = telegram_cfg.get("user_id", 0)
        if self.user_id == 0:
            # 使用目录名作为user_id的备选
            # 修复：多用户分支 - 变量名写错导致 NameError（user_id 缺失时会直接崩溃）。
            dir_name = os.path.basename(self.user_dir)
            try:
                self.user_id = int(dir_name)
            except ValueError:
                # 如果目录名不是数字，使用哈希值
                self.user_id = hash(dir_name) % 100000000
        
        self.config = UserConfig(
            user_id=self.user_id,
            name=account_cfg.get("name", data.get("name", f"用户{self.user_id}")),
            telegram=telegram_cfg,
            groups=groups_cfg,
            zhuque=zhuque_cfg,
            notification=notification_cfg,
            ai=ai_cfg,
        )
        log_event(
            logging.INFO,
            'load_config',
            '加载用户配置成功',
            f'user_id={self.user_id}, name={self.config.name}, file={os.path.basename(config_path)}'
        )

    def get_user_config_path(self) -> str:
        return self._config_path or self._resolve_user_config_path()

    def reload_user_config(self):
        """重新加载账号配置并刷新模型配置。"""
        with self._lock:
            self._load_config()
            self._model_manager_ai_sig = ""
        if self._model_manager is not None:
            self.get_model_manager().load_models()

    def update_ai_config(self, new_ai_config: Dict[str, Any]) -> str:
        """
        更新并持久化账号 ai 配置到对应 *_config.json。
        返回写入路径。
        """
        if not isinstance(new_ai_config, dict):
            raise ValueError("ai 配置必须是对象(dict)")

        with self._lock:
            config_path = self.get_user_config_path()
            data = load_json_with_comments(config_path)
            if not isinstance(data, dict):
                data = {}

            # 统一使用 api_keys 字段，兼容迁移旧字段 api_key。
            ai_data = dict(new_ai_config)
            if "api_key" in ai_data and "api_keys" not in ai_data:
                ai_data["api_keys"] = ai_data.pop("api_key")
            ai_data.pop("api_key", None)
            data["ai"] = ai_data

            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            self._config_path = config_path
            self._config_data = data
            self.config.ai = ai_data
            self._model_manager_ai_sig = ""

        if self._model_manager is not None:
            self.get_model_manager().load_models()
        return config_path
    
    def _load_state(self):
        state_path = os.path.join(self.user_dir, "state.json")
        default_rt = get_default_runtime()
        
        if os.path.exists(state_path):
            try:
                with open(state_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 合并默认运行时变量和保存的运行时变量
                saved_runtime = data.get("runtime", {})
                merged_runtime = {**default_rt, **saved_runtime}
                
                self.state = UserState(
                    history=data.get("history", [])[-2000:],
                    bet_type_history=data.get("bet_type_history", [])[-2000:],
                    predictions=data.get("predictions", [])[-2000:],
                    bet_sequence_log=data.get("bet_sequence_log", [])[-5000:],
                    runtime=merged_runtime
                )
                log_event(logging.DEBUG, 'load_state', '加载用户状态成功', f'user_id={self.user_id}')
            except Exception as e:
                log_event(logging.ERROR, 'load_state', '加载用户状态失败', f'user_id={self.user_id}, error={str(e)}, action=使用默认状态')
                self.state = UserState(runtime=default_rt)
        else:
            self.state = UserState(runtime=default_rt)
            log_event(logging.INFO, 'load_state', '创建新用户状态', f'user_id={self.user_id}')

        # 修复：多用户分支 - 新建用户若历史为空会长期无法下注；兼容导入 master 单用户 state.json 历史。
        if len(self.state.history) < 40:
            self._try_migrate_legacy_state()

    def _try_migrate_legacy_state(self):
        """尝试从根目录 legacy state.json 导入历史与关键运行时字段。"""
        legacy_state_path = "state.json"
        if not os.path.exists(legacy_state_path):
            return False

        # 仅导入与当前账号匹配的单用户状态，避免多账号串状态
        try:
            legacy_cfg_path = os.path.abspath("config.py")
            if not os.path.exists(legacy_cfg_path):
                return False

            spec = importlib.util.spec_from_file_location("ydxbot_legacy_config", legacy_cfg_path)
            if spec is None or spec.loader is None:
                return False
            legacy_config = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(legacy_config)

            if int(getattr(legacy_config, "user", 0)) != int(self.user_id):
                return False
        except Exception:
            return False

        try:
            with open(legacy_state_path, 'r', encoding='utf-8') as f:
                legacy_state = json.load(f)
        except Exception as e:
            log_event(logging.WARNING, 'load_state', '读取legacy状态失败', f'user_id={self.user_id}, error={str(e)}')
            return False

        legacy_history = [x for x in legacy_state.get("history", []) if x in [0, 1]][-2000:]
        if len(legacy_history) < 40:
            return False

        self.state.history = legacy_history
        self.state.bet_type_history = legacy_state.get("bet_type_history", [])[-2000:]
        self.state.predictions = legacy_state.get("predictions", [])[-2000:]
        self.state.bet_sequence_log = legacy_state.get("bet_sequence_log", [])[-5000:]

        legacy_runtime = legacy_state.get("state", {})
        for key in [
            "win_count", "lose_count", "bet_amount", "bet_sequence_count", "win_total",
            "total", "earnings", "period_profit", "explode_count", "mode", "initial_amount",
            "lose_stop", "lose_once", "lose_twice", "lose_three", "lose_four",
            "stop_count", "pause_count", "account_balance", "current_model_id"
        ]:
            if key in legacy_runtime:
                self.state.runtime[key] = legacy_runtime[key]

        log_event(logging.INFO, 'load_state', '已导入legacy单用户状态', f'user_id={self.user_id}, history_len={len(legacy_history)}')
        self.save_state()
        return True
    
    def _load_presets(self):
        presets_path = os.path.join(self.user_dir, "presets.json")
        
        # 内置预设作为权威基线（代码更新后应覆盖同名旧值）
        self.presets = dict(constants.PRESETS)
        
        if os.path.exists(presets_path):
            try:
                with open(presets_path, 'r', encoding='utf-8') as f:
                    user_presets = json.load(f)
                    if not isinstance(user_presets, dict):
                        raise ValueError("presets.json 必须是对象(dict)")

                    overridden_builtins = 0
                    custom_count = 0
                    for key, value in user_presets.items():
                        if key in constants.PRESETS:
                            overridden_builtins += 1
                            continue
                        self.presets[key] = value
                        custom_count += 1

                log_event(
                    logging.DEBUG,
                    'load_presets',
                    '加载用户预设成功',
                    f'user_id={self.user_id}, custom={custom_count}, builtin_refreshed={overridden_builtins}'
                )
            except Exception as e:
                log_event(logging.ERROR, 'load_presets', '加载用户预设失败', f'user_id={self.user_id}, error={str(e)}')
        else:
            log_event(logging.INFO, 'load_presets', '初始化默认预设', f'user_id={self.user_id}')
        
        # 保存合并后的预设到文件（确保文件是最新的）
        self.save_presets()
    
    def save_state(self):
        with self._lock:
            state_path = os.path.join(self.user_dir, "state.json")
            data = {
                "history": self.state.history[-2000:],
                "bet_type_history": self.state.bet_type_history[-2000:],
                "predictions": self.state.predictions[-2000:],
                "bet_sequence_log": self.state.bet_sequence_log[-5000:],
                "runtime": self.state.runtime
            }
            try:
                with open(state_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                log_event(logging.DEBUG, 'save_state', '保存用户状态成功', f'user_id={self.user_id}')
            except Exception as e:
                log_event(logging.ERROR, 'save_state', '保存用户状态失败', f'user_id={self.user_id}, error={str(e)}')
    
    def save_presets(self):
        with self._lock:
            presets_path = os.path.join(self.user_dir, "presets.json")
            try:
                with open(presets_path, 'w', encoding='utf-8') as f:
                    json.dump(self.presets, f, indent=4, ensure_ascii=False)
                log_event(logging.DEBUG, 'save_presets', '保存用户预设成功', f'user_id={self.user_id}')
            except Exception as e:
                log_event(logging.ERROR, 'save_presets', '保存用户预设失败', f'user_id={self.user_id}, error={str(e)}')
    
    def get_runtime(self, key: str, default=None):
        return self.state.runtime.get(key, default)
    
    def set_runtime(self, key: str, value: Any):
        self.state.runtime[key] = value

    def get_model_manager(self):
        """
        获取账号独立模型管理器。
        说明：每个账号持有独立实例，避免多账号并发时模型配置串号。
        """
        ai_cfg = self.config.ai if isinstance(self.config.ai, dict) else {}
        ai_sig = json.dumps(ai_cfg, sort_keys=True, ensure_ascii=False)

        if self._model_manager is None:
            from model_manager import ModelManager
            self._model_manager = ModelManager()

        if ai_sig != self._model_manager_ai_sig:
            self._model_manager.apply_shared_config({"ai": ai_cfg})
            self._model_manager_ai_sig = ai_sig

        return self._model_manager


class UserManager:
    def __init__(self, users_dir: str = "users", config_dir: str = "config", shared_dir: Optional[str] = None):
        self.users_dir = users_dir
        self.config_dir = config_dir
        self.shared_dir = shared_dir
        self.users: Dict[int, UserContext] = {}
        self.global_config: Dict[str, Any] = {}
        log_event(logging.INFO, 'init', '用户管理器初始化', f'users_dir={users_dir}')
    
    def _load_global_config(self):
        candidates: List[str] = []
        # 兼容旧测试/旧部署：若显式传入 shared_dir，则优先读取该路径。
        if self.shared_dir:
            candidates.extend(
                [
                    os.path.join(self.shared_dir, "global.local.json"),
                    os.path.join(self.shared_dir, "global.json"),
                    os.path.join(self.shared_dir, "global.example.json"),
                ]
            )

        candidates.extend(
            [
                os.path.join(self.config_dir, "global_config.json"),
                os.path.join(self.config_dir, "global_config.example.json"),
                # 兼容旧命名
                os.path.join(self.config_dir, "global.json"),
                os.path.join(self.config_dir, "global.example.json"),
                os.path.join("shared", "global.local.json"),
                os.path.join("shared", "global.json"),
                os.path.join("shared", "global.example.json"),
            ]
        )

        chosen_path = ""
        self.global_config = {}
        for path in candidates:
            if os.path.exists(path):
                self.global_config = load_json_with_comments(path)
                chosen_path = path
                break

        if chosen_path:
            if chosen_path.endswith("global.example.json") or chosen_path.endswith("global_config.example.json"):
                log_event(logging.WARNING, 'load_global', '使用示例全局配置（请复制为本地私有配置）', f'path={chosen_path}')
            else:
                log_event(logging.INFO, 'load_global', '加载全局配置成功', f'path={chosen_path}')
        else:
            log_event(logging.WARNING, 'load_global', '全局配置文件不存在', f'checked={candidates}')

        # 同步共享 AI 配置给模型管理器，保证当前运行链路稳定。
        try:
            from model_manager import model_manager
            model_manager.apply_shared_config(self.global_config)
        except Exception as e:
            log_event(logging.WARNING, 'load_global', '同步模型共享配置失败', f'error={str(e)}')
    
    def load_all_users(self) -> int:
        self._load_global_config()
        
        if not os.path.exists(self.users_dir):
            os.makedirs(self.users_dir)
            log_event(logging.INFO, 'load_users', '创建用户目录', f'path={self.users_dir}')
            return 0
        
        loaded_count = 0
        for user_id_str in os.listdir(self.users_dir):
            if user_id_str.startswith('_'):
                continue
            
            user_dir = os.path.join(self.users_dir, user_id_str)
            if os.path.isdir(user_dir):
                try:
                    ctx = UserContext(user_dir, self.global_config)
                    self.users[ctx.user_id] = ctx
                    loaded_count += 1
                except Exception as e:
                    log_event(logging.ERROR, 'load_user', '加载用户失败', f'user_dir={user_dir}, error={str(e)}')
        
        log_event(logging.INFO, 'load_users', '加载用户完成', f'count={loaded_count}')
        return loaded_count
    
    def get_user(self, user_id: int) -> Optional[UserContext]:
        return self.users.get(user_id)
    
    def get_all_users(self) -> Dict[int, UserContext]:
        return self.users
    
    def create_user(self, user_id: int, config: dict) -> UserContext:
        user_dir = os.path.join(self.users_dir, str(user_id))
        os.makedirs(user_dir, exist_ok=True)
        
        dir_name = os.path.basename(os.path.normpath(user_dir))
        config_path = os.path.join(user_dir, f"{dir_name}_config.json")
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        
        ctx = UserContext(user_dir)
        self.users[user_id] = ctx
        log_event(logging.INFO, 'create_user', '创建用户成功', f'user_id={user_id}')
        return ctx
    
    def delete_user(self, user_id: int) -> bool:
        if user_id not in self.users:
            return False
        
        user_dir = os.path.join(self.users_dir, str(user_id))
        import shutil
        shutil.rmtree(user_dir)
        del self.users[user_id]
        log_event(logging.INFO, 'delete_user', '删除用户成功', f'user_id={user_id}')
        return True
    
    def get_button_mapping(self) -> Dict[str, Dict[int, int]]:
        return self.global_config.get("button_mapping", {
            "small": {500: 14, 2000: 12, 20000: 10, 50000: 8, 250000: 6, 1000000: 4, 5000000: 2, 50000000: 0},
            "big": {500: 15, 2000: 13, 20000: 11, 50000: 9, 250000: 7, 1000000: 5, 5000000: 3, 50000000: 1}
        })
    
    def get_iflow_config(self) -> Dict:
        # 兼容：通用配置仍可定义 ai（例如旧版 global/global_config 配置）
        return self.global_config.get("ai") or self.global_config.get("iflow", {})


def migrate_from_legacy(config_module, variable_module) -> UserContext:
    """
    从旧版config.py和variable.py迁移到新结构
    """
    user_id = config_module.user
    user_dir = os.path.join("users", str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    config_data = {
        "user_id": user_id,
        "name": config_module.name,
        "telegram": {
            "api_id": config_module.api_id,
            "api_hash": config_module.api_hash,
            "session_name": config_module.user_session
        },
        "groups": {
            "zq_group": config_module.zq_group,
            "zq_bot": config_module.zq_bot,
            "monitor": getattr(config_module, "monitor", [])
        },
        "zhuque": {
            "cookie": config_module.ZHUQUE_COOKIE,
            "csrf_token": config_module.ZHUQUE_X_CSRF
        },
        "notification": {
            "admin_chat": config_module.user,
            "iyuu": {
                "enable": config_module.iyuu_config.get("enable", False),
                "token": config_module.iyuu_config.get("token", ""),
                "notify_types": config_module.iyuu_config.get("notify_types", [])
            },
            "tg_bot": {
                "enable": config_module.tg_bot_config.get("enable", False),
                "bot_token": config_module.tg_bot_config.get("bot_token", ""),
                "chat_id": config_module.tg_bot_config.get("chat_id", "")
            }
        }
    }
    
    dir_name = os.path.basename(os.path.normpath(user_dir))
    config_path = os.path.join(user_dir, f"{dir_name}_config.json")
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=4, ensure_ascii=False)
    
    state_data = {
        "history": variable_module.history[-2000:],
        "bet_type_history": variable_module.bet_type_history[-2000:],
        "predictions": variable_module.predictions[-2000:],
        "bet_sequence_log": variable_module.bet_sequence_log[-5000:],
        "runtime": {
            "current_round": variable_module.current_round,
            "current_bet_seq": variable_module.current_bet_seq,
            "win_count": variable_module.win_count,
            "lose_count": variable_module.lose_count,
            "bet_amount": variable_module.bet_amount,
            "bet_sequence_count": variable_module.bet_sequence_count,
            "win_total": variable_module.win_total,
            "total": variable_module.total,
            "earnings": variable_module.earnings,
            "period_profit": variable_module.period_profit,
            "explode_count": variable_module.explode_count,
            "stop_count": variable_module.stop_count,
            "gambling_fund": variable_module.gambling_fund,
            "account_balance": variable_module.account_balance,
            "current_model_id": variable_module.current_model_id,
            "current_preset_name": variable_module.current_preset_name,
            "initial_amount": variable_module.initial_amount,
            "lose_stop": variable_module.lose_stop,
            "lose_once": variable_module.lose_once,
            "lose_twice": variable_module.lose_twice,
            "lose_three": variable_module.lose_three,
            "lose_four": variable_module.lose_four,
            "continuous": variable_module.continuous,
            "explode": variable_module.explode,
            "stop": variable_module.stop,
            "profit_stop": variable_module.profit_stop,
            "profit": variable_module.profit
        }
    }
    
    state_path = os.path.join(user_dir, "state.json")
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state_data, f, indent=4, ensure_ascii=False)
    
    presets_data = constants.PRESETS
    presets_path = os.path.join(user_dir, "presets.json")
    with open(presets_path, 'w', encoding='utf-8') as f:
        json.dump(presets_data, f, indent=4, ensure_ascii=False)
    
    log_event(logging.INFO, 'migrate', '迁移完成', f'user_id={user_id}')
    return UserContext(user_dir)
