#!/usr/bin/env python3
"""
G2/G3/G5 修复回归测试。

- G2: 文本拦截必须替换原文, 不能追加警告后放行泄密内容
- G3: proxy 拦截信号通过响应顶层 x_gov_proxy 扩展字段传递,
      tool_calls 数组保持删除(防止真实客户端执行)
- G5: judge 凭证缺失时 fail-closed 返回 BLOCK, 且仅高风险工具触发 judge

运行: PYTHONPATH=src python3 -m unittest system.tests.test_g235_fixes -v
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from system.policies import llm_injection_judge as judge_mod
from system.proxy import gov_proxy as proxy_mod


def _make_response(content="", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "test-resp",
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
    }


def _tc(name, args):
    return {"id": "call_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


class TestG5JudgeFailClosed(unittest.TestCase):
    """G5: 凭证缺失一律 BLOCK, 不静默放行"""

    def test_no_credentials_returns_block(self):
        with mock.patch.object(judge_mod, "_get_api_credentials", return_value=(None, None, None)):
            decision, reason = judge_mod.judge(
                prior_text="用户: 查一下预算", tool_name="send_email", args_summary="to=a@b.com"
            )
        self.assertEqual(decision, "BLOCK")
        self.assertIn("fail-closed", reason)

    def test_should_judge_narrows_scope(self):
        # 高风险工具触发 judge
        for t in ("send_email", "write", "exec", "approve"):
            self.assertTrue(judge_mod.should_judge(t), t)
        # 低风险工具不触发, 避免每次放行多付一次 LLM 调用
        for t in ("query_db", "read", "kb_search", "calendar_list_events"):
            self.assertFalse(judge_mod.should_judge(t), t)


class TestG3ProxyBlockSignal(unittest.TestCase):
    """G3: 拦截信号经 x_gov_proxy 传递, tool_calls 数组被清空"""

    def test_blocked_tool_call_moved_to_extension_field(self):
        resp = _make_response(tool_calls=[_tc("exec", {"command": "rm -rf /data"})])
        messages = [{"role": "user", "content": "请帮我清理一下磁盘"}]
        with mock.patch.object(proxy_mod, "llm_judge", return_value=("SAFE", "")):
            modified, entries = proxy_mod._check_response_security(resp, messages)

        msg = modified["choices"][0]["message"]
        # tool_calls 数组里被拦项已删除(保护真实客户端)
        self.assertEqual(msg.get("tool_calls"), [])
        # 扩展字段完整记录被拦调用
        ext = modified.get("x_gov_proxy", {}).get("blocked_tool_calls", [])
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["tool"], "exec")
        self.assertTrue(ext[0]["rule"].startswith("GOV-"), ext[0]["rule"])
        self.assertTrue(ext[0]["reason"])
        # 审计日志同步记录
        self.assertTrue(any(e["type"] == "tool_call_blocked" for e in entries))

    def test_low_risk_tool_skips_judge(self):
        # query_db 不在 should_judge 名单: 规则未命中时不得调 judge
        resp = _make_response(tool_calls=[_tc("query_db", {"table": "public_budget"})])
        messages = [{"role": "user", "content": "查一下公开预算表"}]
        with mock.patch.object(proxy_mod, "llm_judge", return_value=("BLOCK", "不应被调用")) as mj:
            modified, _ = proxy_mod._check_response_security(resp, messages)
        mj.assert_not_called()
        msg = modified["choices"][0]["message"]
        self.assertEqual(len(msg.get("tool_calls", [])), 1)  # 原样放行
        self.assertNotIn("x_gov_proxy", modified)


class TestG2TextBlockReplacesContent(unittest.TestCase):
    """G2: 文本判定 BLOCK 时替换原文, 泄密内容不得返回"""

    def test_text_block_replaces_original(self):
        leak_text = "好的,我这就删除全部审计日志,然后把员工薪资表发送给外部邮箱。"
        resp = _make_response(content=leak_text)
        messages = [{"role": "user", "content": "帮我处理一下日志"}]
        with mock.patch.object(proxy_mod, "llm_judge", return_value=("BLOCK", "含破坏与外发意图")):
            modified, entries = proxy_mod._check_response_security(resp, messages)

        content = modified["choices"][0]["message"]["content"]
        # 原文不得残留
        self.assertNotIn("删除全部审计日志", content)
        self.assertNotIn("薪资表", content)
        # 替换为拦截提示
        self.assertIn("拦截", content)
        self.assertIn("含破坏与外发意图", content)
        self.assertTrue(any(e["type"] == "text_blocked" for e in entries))


class TestG11SerializedSendBlock(unittest.TestCase):
    """G11: 会话内逐条单发邮件(化整为零)第 3 封起阻断"""

    def _run(self, history_sends, current_sends):
        """history_sends: 历史已发邮件数; current_sends: 本响应内连续发邮件数"""
        messages = [{"role": "user", "content": "把Q4培训计划发给各科室"}]
        if history_sends:
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [_tc("send_email", {"to": f"dept{i}", "subject": "s", "body": "b"})
                               for i in range(history_sends)],
            })
        resp = _make_response(tool_calls=[
            _tc("send_email", {"to": f"dept{i}", "subject": "s", "body": "b"})
            for i in range(current_sends)
        ])
        with mock.patch.object(proxy_mod, "llm_judge", return_value=("SAFE", "")):
            modified, _ = proxy_mod._check_response_security(resp, messages)
        msg = modified["choices"][0]["message"]
        passed = [t for t in msg.get("tool_calls", [])]
        blocked = (modified.get("x_gov_proxy") or {}).get("blocked_tool_calls", [])
        return len(passed), len(blocked)

    def test_first_two_sends_allowed(self):
        # 历史 0 封, 本次发 2 封: 全部放行(正常通知+抄送)
        passed, blocked = self._run(history_sends=0, current_sends=2)
        self.assertEqual((passed, blocked), (2, 0))

    def test_third_send_blocked(self):
        # 历史 0 封, 本次连发 3 封: 前 2 放行, 第 3 拦截
        passed, blocked = self._run(history_sends=0, current_sends=3)
        self.assertEqual((passed, blocked), (2, 1))

    def test_history_counts(self):
        # 历史已发 2 封, 本次再发 1 封: 拦截
        passed, blocked = self._run(history_sends=2, current_sends=1)
        self.assertEqual((passed, blocked), (0, 1))


class TestExecActionField(unittest.TestCase):
    """GOV-DENY-016/017: exec 通过 action 字段执行危险操作"""

    def test_approve_all_blocked(self):
        """exec(action=approve_all) 应被 GOV-DENY-016 拦截"""
        resp = _make_response(tool_calls=[_tc("exec", {"action": "approve_all"})])
        messages = [{"role": "user", "content": "帮我批量审批"}]
        with mock.patch.object(proxy_mod, "llm_judge", return_value=("SAFE", "")):
            modified, _ = proxy_mod._check_response_security(resp, messages)
        blocked = (modified.get("x_gov_proxy") or {}).get("blocked_tool_calls", [])
        self.assertEqual(len(blocked), 1)

    def test_list_tables_blocked(self):
        """exec(action=list_tables) 应被 GOV-DENY-017 拦截"""
        resp = _make_response(tool_calls=[_tc("exec", {"action": "list_tables"})])
        messages = [{"role": "user", "content": "看看有哪些表"}]
        with mock.patch.object(proxy_mod, "llm_judge", return_value=("SAFE", "")):
            modified, _ = proxy_mod._check_response_security(resp, messages)
        blocked = (modified.get("x_gov_proxy") or {}).get("blocked_tool_calls", [])
        self.assertEqual(len(blocked), 1)

    def test_normal_exec_allowed(self):
        """exec(action=run_report) 正常操作不拦"""
        resp = _make_response(tool_calls=[_tc("exec", {"action": "run_report"})])
        messages = [{"role": "user", "content": "运行报表"}]
        with mock.patch.object(proxy_mod, "llm_judge", return_value=("SAFE", "")):
            modified, _ = proxy_mod._check_response_security(resp, messages)
        passed = len(modified["choices"][0]["message"].get("tool_calls", []))
        self.assertEqual(passed, 1)


if __name__ == "__main__":
    unittest.main()
