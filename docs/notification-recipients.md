# 通知接收人计算逻辑梳理

本文档梳理 django-helpdesk 系统中工单通知的接收人计算规则，覆盖工单创建、状态变更、评论、分配、SLA 升级（Escalation）、批量关闭、邮件渠道交互等所有通知触发场景，并对 SSO/OAuth 接入的外部用户场景做专项说明。

---

## 一、核心发送机制：`Ticket.send()` 方法

所有通知最终都通过 `Ticket.send()` 方法统一分发，定义于 `src/helpdesk/models.py:638-694`。

### 1.1 支持的角色（roles）与实际接收人映射

| 角色 key | 实际接收的邮箱来源 | 代码位置 |
|---------|------------------|---------|
| `submitter` | `ticket.submitter_email`（工单提交人邮箱字段） | `models.py:686` |
| `assigned_to` | `ticket.assigned_to.email`（工单负责人关联 User 的 email） | `models.py:689-690` |
| `new_ticket_cc` | `ticket.queue.new_ticket_cc`（Queue 表配置，逗号分隔多邮箱，仅用于新工单场景） | `models.py:688` |
| `ticket_cc` | ① `ticket.queue.updated_ticket_cc`（Queue 级更新抄送，逗号分隔）<br>② 若 `queue.enable_notifications_on_email_events=True`，遍历 `ticket.ticketcc_set.all()` 中每条记录的 `cc.email_address`（工单级订阅者） | `models.py:687`, `models.py:691-693` |

### 1.2 去重机制

方法内部维护 `recipients` 集合：

1. 初始先加入 `dont_send_to` 参数传入的邮箱集合
2. 强制加入 `self.queue.email_address`（避免给队列自身发邮件）
3. 每次发送前判断 `recipient not in recipients`，成功后加入集合

### 1.3 模板与 context

`roles` 参数是一个字典，结构为 `{role_key: (template_name, context_dict)}`。调用方在发起 `ticket.send()` 之前根据业务场景决定哪些角色应该收到通知，以及各自使用何种邮件模板。

---

## 二、各场景通知接收人规则

### 2.1 工单创建（Web 渠道）

代码位置：`src/helpdesk/forms.py:416-434`（`TicketForm._send_messages`）

| 角色 | 是否始终发送 | 附加条件 | 邮件模板 |
|------|:----------:|---------|---------|
| `submitter` | ✅ | — | `newticket_submitter` |
| `new_ticket_cc` | ✅ | — | `newticket_cc` |
| `ticket_cc` | ✅ | — | `newticket_cc` |
| `assigned_to` | ❌ | `ticket.assigned_to` 存在 **且** `assigned_to.usersettings_helpdesk.email_on_ticket_assign=True` | `assigned_owner` |

### 2.2 工单更新（评论 / 状态变更 / 重新分配）

代码位置：`src/helpdesk/update_ticket.py:128-188`（`process_email_notifications_for_ticket_update`）

#### 前置短路条件
- 若全局配置 `HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS=True` **且** 本次 follow-up 的 `public=False`（私有评论），则**跳过所有邮件通知**，直接 return。（`update_ticket.py:141-142`）

#### Submitter（提交人）
条件满足任一即发送（`update_ticket.py:145-151`）：
- 全局开关 `HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES=True`
- 或者：follow-up 为 `public=True` **并且**（存在非空 `comment` 或者新状态是 `RESOLVED_STATUS` 或 `CLOSED_STATUS`）

模板：固定 `updated_submitter`（为保持向后兼容，不使用动态前缀）

#### Assigned To（负责人）
条件满足任一即发送（`update_ticket.py:164-170`）：
- 该负责人 `usersettings_helpdesk.email_on_ticket_change=True`
- 或者：本次为重新分配（`reassigned=True`）**且** 该负责人 `usersettings_helpdesk.email_on_ticket_assign=True`

模板前缀由 `get_email_template_prefix()` 动态决定（`update_ticket.py:190-198`）：

| 场景 | 前缀 | 完整模板名 |
|------|-----|----------|
| 重新分配（reassigned=True） | `assigned_` | `assigned_owner` |
| 新状态 = RESOLVED | `resolved_` | `resolved_owner` |
| 新状态 = CLOSED | `closed_` | `closed_owner` |
| 其他更新 | `updated_` | `updated_owner` |

#### Ticket CC（抄送）
无条件发送，模板使用与上面相同的动态前缀 + `cc`，例如 `updated_cc`、`resolved_cc`、`closed_cc`、`assigned_cc`。

### 2.3 分配的特殊判定

代码位置：`src/helpdesk/update_ticket.py:264-282`

- **分配给新用户**：设 `reassigned=True`，触发「重新分配」分支的模板前缀与条件检查
- **取消分配（owner=0）**：只更新字段，不设 `reassigned=True`，因此 **取消分配不会触发重新分配通知**

### 2.4 SLA 自动升级（Escalation）

代码位置：`src/helpdesk/management/commands/escalate_tickets.py:86-95`

**注意**：SLA 升级场景对 `assigned_to` 发送 **没有检查 `usersettings_helpdesk.email_on_ticket_change` / `email_on_ticket_assign`**，只要有负责人就发送。

| 角色 | 条件 | 模板 |
|------|-----|------|
| `submitter` | 始终 | `escalated_submitter` |
| `ticket_cc` | 始终 | `escalated_cc` |
| `assigned_to` | `ticket.assigned_to` 存在即发送（**绕过 UserSettings 检查**） | `escalated_owner` |

### 2.5 批量关闭工单

代码位置：`src/helpdesk/views/staff.py:843-860`

| 角色 | 条件 | 模板 |
|------|-----|------|
| `submitter` | 始终 | `closed_submitter` |
| `ticket_cc` | 始终 | `closed_cc` |
| `assigned_to` | `assigned_to` 存在 **且** `usersettings_helpdesk.email_on_ticket_change=True` | `closed_owner` |

### 2.6 邮件渠道创建 / 更新工单

代码位置：`src/helpdesk/email.py:717-754`（`send_info_email`）

#### 新工单（邮件创建）
| 角色 | 模板 |
|------|------|
| `submitter` | `newticket_submitter` |
| `new_ticket_cc` | `newticket_cc` |
| `ticket_cc` | `newticket_cc` |

#### 更新已有工单（邮件回复）
| 角色 | 条件 | 模板 |
|------|-----|------|
| `submitter` | 始终 | `updated_submitter` |
| `assigned_to` | 始终（**绕过 UserSettings 检查**） | `updated_owner` |
| `ticket_cc` | 仅当 `queue.enable_notifications_on_email_events=True` | `updated_cc` |

#### 自动回复保护
若 `is_autoreply(message)` 检测为自动回复邮件（含有 `Auto-Submitted` 头、`X-Auto-Response-Suppress: DR/AutoReply/All`、或 `List-Id`/`List-Unsubscribe`），则**完全不发送任何回复**，避免邮件循环风暴。（`email.py:522-538`, `email.py:701-707`）

---

## 三、关键影响性配置汇总

| 配置项 | 位置 | 作用 |
|-------|------|------|
| `HELPDESK_AUTO_SUBSCRIBE_ON_TICKET_RESPONSE` | `settings.py` | 员工回复工单时是否自动加入 TicketCC |
| `HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS` | `settings.py` | 私有 follow-up 是否完全跳过所有邮件 |
| `HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES` | `settings.py` | 提交人是否接收所有变更（不限制为公开评论/关闭） |
| `usersettings.email_on_ticket_change` | `UserSettings` 模型 | 负责人是否接收工单变更邮件 |
| `usersettings.email_on_ticket_assign` | `UserSettings` 模型 | 用户被分配工单时是否接收邮件 |
| `queue.new_ticket_cc` | `Queue` 模型 | 队列级「新工单抄送」邮箱（逗号分隔） |
| `queue.updated_ticket_cc` | `Queue` 模型 | 队列级「所有更新抄送」邮箱（逗号分隔） |
| `queue.enable_notifications_on_email_events` | `Queue` 模型 | 是否将工单级 `ticket.ticketcc_set` 订阅者纳入邮件通知（含邮件渠道+内部触发的所有场景） |
| `queue.escalate_days` | `Queue` 模型 | SLA 自动升级周期（天），0 或空表示不启用 |
| `DEFAULT_USER_SETTINGS` | `settings.py:16-22` | UserSettings 字段默认值来源字典，可被 `settings.HELPDESK_DEFAULT_SETTINGS` 覆盖 |

---

## 四、SSO / OAuth 外部用户接入专项说明

> **本章节针对运维上线 SSO（如 LDAP、OIDC、OAuth、SAML 等）后，通过统一认证首次登录的外部用户（无本地手动创建流程）场景。**

### 4.1 背景：UserSettings 的创建时机

`UserSettings` 与 `AUTH_USER_MODEL` 通过 `OneToOneField` 关联（`related_name="usersettings_helpdesk"`），定义于 `src/helpdesk/models.py:1710-1799`。其创建有**三条路径**，但并非所有路径对 SSO 用户都可靠：

#### 路径 1：`post_save` 信号（User 新建时）
```python
# src/helpdesk/models.py:1786-1799
def create_usersettings(sender, instance, created, **kwargs):
    if created:  # ⚠️ 仅当 created=True（User 首次被 save）
        UserSettings.objects.create(user=instance)

models.signals.post_save.connect(create_usersettings, sender=settings.AUTH_USER_MODEL)
```

- 对 **django-allauth / django-oauth-toolkit 等标准 OAuth/OIDC 后端首次登录自动创建 User** 的情况：有效，因为后端会走 `User.objects.create()` → `save(force_insert=True)` → 触发 `post_save(created=True)`。
- 对 **预先同步到 auth_user 表的 LDAP/AD 用户**（如通过 `django-auth-ldap` 的 `populate_user` + 自定义脚本批量导入）：**不可靠**，批量 `bulk_create` / 原生 SQL 插入不触发 `post_save`，或已存在用户的后续 save 不会带 `created=True`。
- 对 **SSO 登录时用户已在 auth_user 表存在**（历史遗留、其他系统预建）：**不触发**，因为 `created=False`。

#### 路径 2：管理命令 `create_usersettings`（推荐运维兜底）
```python
# src/helpdesk/management/commands/create_usersettings.py:30-33
def handle(self, *args, **options):
    for u in User.objects.all():
        UserSettings.objects.get_or_create(user=u)  # ✅ 使用 get_or_create，幂等安全
```

**运维上线 SSO 后必须立即执行一次**，之后建议纳入 cron 或部署流水线定期执行（如每日凌晨），确保任何来源的 User 都补全 UserSettings：
```bash
python manage.py create_usersettings
```

#### 路径 3：数据库迁移 `0002_populate_usersettings`
仅在安装 / 升级时对当时已存在的 User 做一次性补全（`migrations/0002_populate_usersettings.py:33-37`），SSO 新用户不在覆盖范围内。

### 4.2 默认值来源（`email_on_ticket_change` / `email_on_ticket_assign`）

默认值字典定义在 `src/helpdesk/settings.py:16-22`：
```python
# src/helpdesk/settings.py:16-22
DEFAULT_USER_SETTINGS = {
    "login_view_ticketlist": True,
    "email_on_ticket_change": True,     # ← 外部用户缺省 = 接收变更邮件
    "email_on_ticket_assign": True,     # ← 外部用户缺省 = 接收分配邮件
    "tickets_per_page": 25,
    "use_email_as_submitter": True,
}

# 可在项目 settings.py 中覆盖：
try:
    DEFAULT_USER_SETTINGS.update(settings.HELPDESK_DEFAULT_SETTINGS)
except AttributeError:
    pass
```

模型字段通过 `default` 回调读取（`src/helpdesk/models.py:1684-1698`）：
```python
# src/helpdesk/models.py:1684-1698
def get_default_setting(setting):
    from helpdesk.settings import DEFAULT_USER_SETTINGS
    return DEFAULT_USER_SETTINGS[setting]

def email_on_ticket_change_default():
    return get_default_setting("email_on_ticket_change")

def email_on_ticket_assign_default():
    return get_default_setting("email_on_ticket_assign")
```

**对 SSO 外部用户的实际效果**：
- 若 UserSettings 已创建（走路径 1 / 2 / 3）：两字段默认均为 `True`，即默认**开通**所有通知
- 运维若希望外部用户默认关闭，可在 `settings.py` 中配置 `HELPDESK_DEFAULT_SETTINGS = {"email_on_ticket_change": False, "email_on_ticket_assign": False, ...}`，但**只影响此后新创建的 UserSettings**，已存在的记录需手动 UPDATE

### 4.3 外部用户被作为 `ticket_cc`（工单订阅者）时的判定

`TicketCC` 模型**不依赖 UserSettings**，因此对 SSO 外部用户完全可用。邮箱地址通过属性 `email_address` 解析（`src/helpdesk/models.py:1946-1952`）：
```python
# src/helpdesk/models.py:1946-1952
def _email_address(self):
    if self.user and self.user.email is not None:
        return self.user.email   # ← 关联 User 时取其 email（SSO 用户场景）
    else:
        return self.email         # ← 否则用裸邮箱字段

email_address = property(_email_address)
```

**是否能收到通知由两层开关决定**：

| 层级 | 开关 | 位置 | 说明 |
|-----|------|------|------|
| 队列级 | `queue.enable_notifications_on_email_events` | `Queue.enable_notifications_on_email_events` | 该开关为 `False` 时，`Ticket.send()` **完全不遍历** `ticket.ticketcc_set.all()`（`models.py:691-693`） |
| 场景级 | 调用方 `roles` 字典是否包含 `ticket_cc` key | 见各场景表 | 例如 SLA 升级、工单更新、批量关闭、邮件创建更新等均会带上 `ticket_cc`；但如果调用方没传该角色，则即使订阅了也不会发送 |

**对 SSO 外部用户的判定示例**：
- 外部用户 A 通过 SSO 登录后，自己点「Subscribe」加入 TicketCC（`update_ticket.py:75-95` 的 `subscribe_to_ticket_updates`）
- 其所属 Queue 配置了 `enable_notifications_on_email_events=True`
- 则：他会在所有 **传了 `ticket_cc` 角色** 的通知场景中收到邮件，**与 UserSettings 是否存在无关**（因为走的是 `TicketCC.email_address`，不访问 `.usersettings_helpdesk`）

### 4.4 外部用户被赋为 `assigned_to`（负责人）时的缺省行为

**⚠️ 高危警告：此路径依赖 `.usersettings_helpdesk`，若 UserSettings 不存在会抛出 `RelatedObjectDoesNotExist` 异常。**

访问代码（无任何 try/except 兜底）：
- `src/helpdesk/update_ticket.py:164-170`
- `src/helpdesk/forms.py:425-428`
- `src/helpdesk/views/staff.py:849-852`

示例（`update_ticket.py:164-170`）：
```python
# src/helpdesk/update_ticket.py:164-170
if ticket.assigned_to and (
    ticket.assigned_to.usersettings_helpdesk.email_on_ticket_change   # ⚠️ 直接访问反向外键
    or (
        reassigned
        and ticket.assigned_to.usersettings_helpdesk.email_on_ticket_assign  # ⚠️ 同上
    )
):
    # 发送 assigned_to 通知...
```

OneToOneField 反向访问在关联对象不存在时抛 `RelatedObjectDoesNotExist`（`UserSettings.DoesNotExist` 子类），不是返回 `None`。因此若 SSO 用户还没有 UserSettings：
- 将其设为 `assigned_to` 时，**工单更新流程会直接 500**
- 邮件渠道的更新场景（`email.py:744-745`）**例外**，因为它对 `assigned_to` 不做 UserSettings 检查而直接发送

**结论：上线 SSO 前未跑 `create_usersettings` 属于生产事故级别风险。**

UserSettings 存在时，两字段默认值按 §4.2 的 `DEFAULT_USER_SETTINGS` 生效，即：
- `email_on_ticket_change=True` → 接收变更通知
- `email_on_ticket_assign=True` → 接收分配通知

**例外场景（不检查 UserSettings，负责人必然收到邮件）**：
- SLA 自动升级（`escalate_tickets.py:88-95`）：`assigned_to` 角色无条件加入 roles
- 邮件渠道更新工单（`email.py:744-745`）：`assigned_to` 角色无条件加入 roles

### 4.5 SSO 首次登录后访问各页面触发 UserSettings 自动创建的时机

**结论：标准页面访问流程不会主动调用 `get_or_create` 补全 UserSettings。**

现有代码中对 `usersettings_helpdesk` 的访问仅在 `src/helpdesk/views/public.py:72-78` 做了 `UserSettings.DoesNotExist` 异常捕获，但**仅 fallback 到 dashboard，不做创建动作**：

```python
# src/helpdesk/views/public.py:72-78
try:
    if request.user.usersettings_helpdesk.login_view_ticketlist:
        return HttpResponseRedirect(reverse("helpdesk:list"))
    else:
        return HttpResponseRedirect(reverse("helpdesk:dashboard"))
except UserSettings.DoesNotExist:     # ← 只兜底，不创建
    return HttpResponseRedirect(reverse("helpdesk:dashboard"))
```

其他视图（如 `views/staff.py:157-160` Dashboard）只做 `hasattr` 防御式检查，也不会创建：
```python
# src/helpdesk/views/staff.py:157-160
if request.user.is_authenticated and hasattr(request.user, "usersettings_helpdesk"):
    tickets_per_page = request.user.usersettings_helpdesk.tickets_per_page
else:
    tickets_per_page = 25   # ← fallback，不创建
```

**因此：SSO 用户首次登录后，系统并不会自动补全 UserSettings，只能依赖以下两种方式之一：**

1. **推荐**：运维跑 `python manage.py create_usersettings`（内部 `UserSettings.objects.get_or_create(user=u)`，见 `management/commands/create_usersettings.py:30-33`）
2. **可选**：在项目中自行接入 `user_logged_in` signal，在登录回调中调用 `UserSettings.objects.get_or_create(user=request.user)`。注意信号处理中不要调用 user.save() 以免触发 post_save 递归。

### 4.6 运维上线 SSO 操作 Checklist

| 步骤 | 操作 | 目的 |
|-----|------|------|
| 1 | `settings.py` 中确认 `HELPDESK_DEFAULT_SETTINGS` 是否需要覆盖外部用户默认通知开关 | 决定新创建 UserSettings 的 `email_on_ticket_change` / `email_on_ticket_assign` 缺省值 |
| 2 | SSO 后端配置完成后、放流量前，执行 `python manage.py create_usersettings` | 为所有已存在于 auth_user 的 SSO 预同步用户补全 UserSettings |
| 3 | 将 `python manage.py create_usersettings` 加入 cron（推荐每日一次）或接入部署后钩子 | 捕获任何绕过 post_save signal 的新建 User（LDAP 批处理、第三方同步等） |
| 4 | 确认需要发送 TicketCC 邮件的 Queue 都已开启 `enable_notifications_on_email_events` | 确保工单级订阅者（含 SSO 外部用户）能收到通知 |
| 5 | 冒烟测试：新建一个尚未登录过的 SSO 用户 → 将其设为 ticket 负责人 → 触发一次评论更新 / 重新分配 → 验证无 500 且邮件送达 | 验证 §4.4 的 `RelatedObjectDoesNotExist` 风险已被消除 |

### 4.7 快速排障参考

| 现象 | 最可能原因 | 排查方向 |
|-----|----------|---------|
| 给 SSO 用户分配工单后，Web 端评论时返回 500 | 该用户无 UserSettings，`assigned_to.usersettings_helpdesk` 抛 `RelatedObjectDoesNotExist` | 查 `UserSettings.objects.filter(user=xxx).exists()`，不存在则立即跑 `create_usersettings` |
| SSO 用户是 TicketCC 但从未收到任何邮件 | Queue 的 `enable_notifications_on_email_events=False`，或该场景调用方 roles 没传 `ticket_cc` | 先查 Queue 配置；若 OK 再查对应场景 roles（例如「仅新工单」场景传了 `new_ticket_cc` 但后续更新无） |
| SSO 用户是负责人，SLA 升级时能收到邮件，但评论更新时收不到 | UserSettings 的 `email_on_ticket_change=False`（被用户或管理员改过），而 SLA 升级路径绕过检查 | 查 `UserSettings.objects.get(user=xxx).email_on_ticket_change` |
| 刚切 SSO 的用户首次登录后，Dashboard 分页条数始终是 25，修改无效 | 视图 `hasattr` 判定为 False，fallback 到硬编码 25，因为 UserSettings 不存在 | 跑 `create_usersettings`，或在登录信号中补 `get_or_create` |
