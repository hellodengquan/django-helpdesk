# 工单状态、评论、分配与 SLA 提醒通知接收人计算逻辑梳理

本文档梳理 django-helpdesk 在工单状态变更、评论、分配以及 SLA 升级四类场景下，通知接收人的计算口径，输出可用的查阅地图。

## 一、核心角色与来源
- **Submitter**：提交人，邮件来自 `Ticket.submitter_email`。
- **Assigned To**：负责人，`Ticket.assigned_to` 关联 User。其 `usersettings` 控制是否订阅。
- **Queue CC**：队列级抄送，`Queue.new_ticket_cc` 与 `Queue.updated_ticket_cc`。
- **Ticket CC**：工单级订阅人集合，通过 `TicketCC` 模型管理，受 `queue.enable_notifications_on_email_events` 总开关控制。

## 二、状态变更与评论（lib.send_templated_mail / send_email_thread）
代码入口：`helpdesk/lib.py`，`helpdesk/views/staff.py:update_ticket`。
- **submitter**：始终送达；存在 `HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES` 时把"仅公开评论或关闭"扩展为"所有变更"。
- **assigned_to**：`usersettings.email_on_ticket_change=True` 触发；分配变更时若 `email_on_ticket_assign=True` 也触发。
- **ticket_cc**：默认参与，但若 `private follow-up` 且 `HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS=True` 则跳过所有外发。

## 三、分配变更（views/staff.py:update_ticket、批量赋值）
- 新分配人：必须满足 `assigned_to.usersettings.email_on_ticket_assign=True`，模板 `assigned_owner`。
- 旧分配人：若仍是参与者且开关同上，按更新模板送达。
- 私有评论附带分配变更：仍受 `HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS` 约束。

## 四、SLA 升级（management/commands/escalate_tickets.py）
- 触发条件：队列 `escalate_days` 与工单实际未关闭天数对比。
- 接收人：
  - submitter：始终
  - assigned_to：始终（不再受 `email_on_ticket_change` 拦截）
  - ticket_cc：常规拼装
  - new_ticket_cc：队列字段
- 模板：`escalated_submitter` / `escalated_owner` / `escalated_cc`。

## 五、邮件渠道（email.send_info_email）
代码位置：`helpdesk/email.py:717-754`。
- **新工单**：`submitter` → `newticket_submitter`，`new_ticket_cc` 与 `ticket_cc` → `newticket_cc`。
- **更新已有工单（邮件回复）**：`submitter` → `updated_submitter`；`assigned_to` → `updated_owner`；`ticket_cc` 仅当 `queue.enable_notifications_on_email_events=True` 时 → `updated_cc`。
- **autoreply**：`is_autoreply=True` 完全跳过外发，避免循环。

## 六、批量关闭（views/staff.py:843-860）
- submitter / ticket_cc：始终送达。
- assigned_to：存在且 `email_on_ticket_change=True` 触发。

## 七、关键设置汇总
| 设置 | 位置 | 作用 |
|---|---|---|
| HELPDESK_AUTO_SUBSCRIBE_ON_TICKET_RESPONSE | settings | 员工回复自动订阅到 TicketCC |
| HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS | settings | 私有 follow-up 完全跳过邮件 |
| HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES | settings | 提交人是否接收所有变更通知 |
| usersettings.email_on_ticket_change | User 设置 | 负责人是否接收变更邮件 |
| usersettings.email_on_ticket_assign | User 设置 | 用户被分配时是否接收邮件 |
| queue.new_ticket_cc / updated_ticket_cc | Queue | 队列级抄送 |
| queue.enable_notifications_on_email_events | Queue | 工单级 TicketCC 是否参与邮件通知 |
| queue.escalate_days | Queue | SLA 自动升级周期 |

## 八、完整决策路径
```
触发事件
├─ 私有 follow-up + HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS → 全部跳过
├─ Submitter
│   ├─ 新工单：始终
│   ├─ 工单更新：公开+评论/关闭 或 全局开关
│   ├─ SLA 升级：始终
│   └─ 批量关闭：始终
├─ Assigned To
│   ├─ 新工单：assigned_to 存在 + email_on_ticket_assign
│   ├─ 工单更新：email_on_ticket_change 或 (分配变更 + email_on_ticket_assign)
│   ├─ SLA 升级：始终
│   └─ 批量关闭：assigned_to 存在 + email_on_ticket_change
├─ Queue CC
│   ├─ new_ticket_cc：仅新工单
│   └─ updated_ticket_cc：所有含 ticket_cc 角色的更新
└─ Ticket CC：仅当 queue.enable_notifications_on_email_events=True 且场景含 ticket_cc
```
