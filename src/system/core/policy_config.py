"""策略配置外置 (#7): JSON 配置 + 版本号 + mtime 热加载。

解决的问题: 数据分级、可信域、审批阈值、处置映射原本写死在 Python 里,
不同政务单位落地时要改代码。抽出为 config/policy_config.json 后:
  - 按部署实例调整 (org_name/instance_id 标识)
  - 版本号追溯 (version)
  - 热加载 (mtime 变更自动生效, 无需重启代理)
  - 代码内置默认值兜底 (配置缺失/损坏时用默认继续跑)

格式: JSON (零依赖, 学生机免装 yaml)。字段缺省时逐项回退默认。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "policy_config.json"

DEFAULTS: Dict[str, Any] = {
    "version": "builtin-default",
    "deployment": {"org_name": "默认部署", "instance_id": "default"},
    "disposition": {
        "NORMAL_OPERATION": "L0",
        "RECONNAISSANCE": "L1",
        "DATA_EXPORT": "L2",
        "AUTHORITY_OPERATION": "L2",
        "DESTRUCTIVE_ACTION": "L3",
        "SYSTEM_TAMPERING": "L3",
        # 就地批量改写与正当业务同形 → 待人工确认 (2026-08-23 加固决策)
        "DATA_TAMPERING": "L2",
    },
    "thresholds": {
        "recon_escalate_n": 3,
        "recon_window_s": 1200,
        "taint_window": 8,
        "approval_ttl_s": 600,
        "judge_confirm_score": 8,
        "judge_release_score": 2,
    },
    "trust": {
        "internal_domain_suffixes": [".gov.cn", ".gov", ".internal", ".local", ".lan", ".intra"],
        "skill_path_markers": ["skills/"],
    },
    "data_classification": [
        {"level": "机密", "patterns": ["secrecy", "涉密", "id_card", "身份证", "机密"]},
        {"level": "敏感", "patterns": [
            "salary", "薪资", "工资", "personnel", "人员", "名册", "台账",
            "contract", "合同", "budget", "预算", "audit", "审计", "报销",
            "信访", "petition", "财务",
        ]},
        {"level": "内部", "patterns": [
            "notice", "通知", "doc", "公文", "draft", "草稿", "meeting", "会议",
            "register", "登记", "/data/", "/registers/",
        ]},
        {"level": "公开", "patterns": ["public", "公告", "公示", "公开"]},
    ],
    "data_default_level": "内部",
    "impact": {
        "wildcard_markers": ["*", "?", "-r", "-rf", "-R", "--recursive", "-delete"],
        "irreversible_patterns": ["rm ", "shred", "truncate", "DELETE FROM", "DROP", "VACUUM", "sed -i"],
    },
    "harm_adjust": {
        "destructive_downgrade": {"max_scope": "single", "max_data_level": "内部", "to": "L2"},
        "data_export_min_levels": ["敏感", "机密"],
    },
    "recon": {
        "sensitive_patterns": [
            "/data", "/registers", "/archive", "/official", "/policy",
            "/finance", "/audit", "/salary", "/secrecy",
            "台账", "名册", "薪资", "合同", "审计", "涉密", "预算", "报销", "信访",
            "财务科", "人事科", "档案室", "信息科", "办公室", "会务科",
            "资产科", "法规科", "信访办", "机关党委",
        ],
    },
    "stream": {"passthrough": True},
}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _fresh_defaults() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULTS))


class PolicyConfig:
    """单例配置: mtime 热加载 + 默认值兜底。"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = Path(path)
        self._cache: Dict[str, Any] = _fresh_defaults()
        self._mtime: float = -1.0
        self._loaded_at: float = 0.0
        self._force_reload()

    @classmethod
    def instance(cls) -> "PolicyConfig":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _force_reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return  # 配置文件不存在, 用默认值
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._cache = _deep_merge(_fresh_defaults(), raw)
            self._mtime = mtime
            self._loaded_at = time.time()
        except (json.JSONDecodeError, OSError) as e:
            print(f"[policy_config] ⚠️ 配置加载失败({e}), 沿用当前配置")

    def _maybe_reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime and time.time() - self._loaded_at > 1:
            self._force_reload()

    def get(self, dotted: str, default: Any = None) -> Any:
        """按点路径读取: get('thresholds.recon_escalate_n')"""
        self._maybe_reload()
        node: Any = self._cache
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    @property
    def version(self) -> str:
        return str(self.get("version", "?"))

    def disposition_for(self, intent: str) -> str:
        return str(self.get(f"disposition.{intent}", "L0"))


def get_config() -> PolicyConfig:
    """模块级访问入口。"""
    return PolicyConfig.instance()
