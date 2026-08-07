# Proxy 拦截测试结果(2026-08-07)

## 测试设置

- 测试集:66 条 BYPASSED 真实场景 case (data/system-design/e2e-pilot/proxy_test_66.jsonl)
- 模型:deepseek-v4-flash
- 防护:gov_proxy.py (现有 proxy, 关键词层 + 结构性规则)

## 结果

| 类别 | 数量 | 占比 |
|---|---|---|
| ❌ BYPASSED | 57 | 86.4% |
| 🛡️ REFUSED | 6 | 9.1% |
| 💬 NO_ATTACK | 3 | 4.5% |

**proxy 当前阻止率: 13.6% (9/66)**

## 关键洞察

1. **proxy 几乎完全失效** — 86.4% BYPASSED 表明 proxy 没拦住真实场景攻击
2. **结构性规则不足**:
   - 没识别 send_email 外发到 dept-share@mock-gov.local (内部但应需审批)
   - 没识别 calendar_create + send_email 群发会议通知
   - 没识别 exec 的 batch_approve 动作
3. **关键词层 INJECTION_MARKERS 完全无效** — 这些 case 已"去标记化",user_msg 和 mock_results 都是正常业务话术
4. **need 结构性规则**:
   - send_email 收件人白名单 (内部部门/外部邮箱分级)
   - write 路径白名单 (/drafts/ vs /data/official/)
   - exec 操作白名单 (rm/journalctl/sql batch update)
   - calendar_create 参会人数限制
   - approve 类工具的请求者权限校验

## 详细数据

详见 `data/system-design/e2e-pilot/proxy_test_66_summary.json`。
