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

---

## 五、SSO 上线执行清单（运维按步骤执行版）

> 本章节为**可直接落地的操作清单**，按时间顺序排列。每一步均附带具体命令与校验方法，适合运维同学对照执行。

### 5.1 上线前：配置确认（放流量前 1 天完成）

#### 5.1.1 全局 settings 项核对

在项目 `settings.py`（或部署环境变量注入的配置文件）中检查以下项，按业务预期设置：

| settings 项 | 建议值 | 影响范围 | 代码位置 |
|------------|--------|---------|---------|
| `HELPDESK_DEFAULT_SETTINGS["email_on_ticket_change"]` | 按需，默认 `True` | 所有新创建 UserSettings 的「工单变更通知」缺省开关 | `src/helpdesk/settings.py:16-22` |
| `HELPDESK_DEFAULT_SETTINGS["email_on_ticket_assign"]` | 按需，默认 `True` | 所有新创建 UserSettings 的「工单分配通知」缺省开关 | `src/helpdesk/settings.py:16-22` |
| `HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES` | 按需，默认 `False` | 提交人（submitter）是否接收所有变更（不限于公开评论/关闭） | `src/helpdesk/update_ticket.py:145-151` |
| `HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS` | 按需，默认 `False` | 私有 follow-up 是否完全跳过邮件 | `src/helpdesk/update_ticket.py:141-142` |
| `HELPDESK_AUTO_SUBSCRIBE_ON_TICKET_RESPONSE` | 按需，默认 `False` | 员工回复时是否自动加入 TicketCC | `src/helpdesk/update_ticket.py:24-32` |

**核对命令**（在 django shell 中执行）：
```bash
cd /path/to/project
python manage.py shell
```
```python
from helpdesk.settings import DEFAULT_USER_SETTINGS, HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES
print("DEFAULT_USER_SETTINGS:", DEFAULT_USER_SETTINGS)
print("HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES:", HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES)
```

#### 5.1.2 Queue 级别 `enable_notifications_on_email_events` 核对

**重要**：该开关决定工单级 TicketCC 订阅者（含 SSO 外部用户）能否收到邮件。若为 `False`，即使订阅了也不会发。（`src/helpdesk/models.py:691-693`）

**列出所有 Queue 当前配置**：
```bash
python manage.py shell -c "
from helpdesk.models import Queue
for q in Queue.objects.all():
    print(f'Queue: {q.slug:20s} | enable_notifications_on_email_events: {q.enable_notifications_on_email_events} | new_ticket_cc: {q.new_ticket_cc or \"(none)\"} | updated_ticket_cc: {q.updated_ticket_cc or \"(none)\"}')
"
```

**批量开启（如果业务需要所有队列都开启 CC 通知）**：
```bash
python manage.py shell -c "
from helpdesk.models import Queue
Queue.objects.all().update(enable_notifications_on_email_events=True)
print('已对所有队列开启 enable_notifications_on_email_events')
"
```

**仅针对特定队列开启**：
```bash
# 将 'support' 'billing' 替换为实际队列 slug
python manage.py shell -c "
from helpdesk.models import Queue
Queue.objects.filter(slug__in=['support', 'billing']).update(enable_notifications_on_email_events=True)
print('Done')
"
```

### 5.2 上线当天：首次补全 UserSettings（放流量前必须执行）

#### 5.2.1 执行一次性补全

**命令**：
```bash
cd /path/to/project
python manage.py create_usersettings
```

内部实现（`src/helpdesk/management/commands/create_usersettings.py:30-33`）：
```python
for u in User.objects.all():
    UserSettings.objects.get_or_create(user=u)  # 幂等，已存在的不改动
```

#### 5.2.2 校验补全结果

```bash
python manage.py shell -c "
from django.contrib.auth import get_user_model
from helpdesk.models import UserSettings
User = get_user_model()
total_users = User.objects.count()
total_settings = UserSettings.objects.count()
missing = total_users - total_settings
print(f'总用户数: {total_users}')
print(f'已创建 UserSettings 数: {total_settings}')
print(f'缺失数: {missing}')
if missing > 0:
    missing_users = User.objects.exclude(pk__in=UserSettings.objects.values_list('user_id', flat=True))
    print('缺失的用户:', [u.username for u in missing_users])
"
```

**预期结果**：`缺失数: 0`。若仍有缺失，排查是否为 `is_active=False` 用户（一般不影响，因为 inactive 用户不会被分配工单，但建议都补齐以防万一）。

### 5.3 上线后：配置 Cron 定期补全

#### 5.3.1 推荐频率：每天凌晨 03:00 执行一次

**添加 crontab**：
```bash
crontab -e
```

加入以下行（替换为实际项目路径和虚拟环境）：
```cron
# django-helpdesk: 每日凌晨补全 UserSettings，确保 SSO/LDAP 同步的新用户都有 settings
0 3 * * * cd /path/to/project && /path/to/venv/bin/python manage.py create_usersettings >> /var/log/helpdesk-create-usersettings.log 2>&1
```

#### 5.3.2 为什么选每天一次
- SSO 新用户可能在任意时间通过同步脚本进入 `auth_user` 表，但 `bulk_create` / 原生 SQL 不会触发 `post_save` 信号（`src/helpdesk/models.py:1786-1799`），导致 UserSettings 缺失
- 每天一次的频率足以覆盖绝大多数场景，同时避免频繁执行带来的开销
- 如果你的 SSO 用户同步是实时的且量很大，可以提升到每小时一次，但一般没必要

#### 5.3.3 验证 cron 是否生效

第二天查看日志：
```bash
grep -i "error\|exception" /var/log/helpdesk-create-usersettings.log
# 或直接看日志长度是否每天增长
ls -lh /var/log/helpdesk-create-usersettings.log
```

### 5.4 灰度回退：安全停用 / 清理指南

#### 5.4.1 临时停用 cron（不删除历史数据）

如果 SSO 回退，只是**不想再新增** UserSettings，但历史数据保留（推荐）：
```bash
crontab -e
# 注释掉或删除 create_usersettings 那一行即可
```

历史 `UserSettings` 记录**不要删**，原因：
- 已分配工单的负责人如果 UserSettings 被删，下一次工单更新会 500（`src/helpdesk/update_ticket.py:164-170` 直接访问反向外键无兜底）
- 已订阅 TicketCC 的用户不受影响（TicketCC 不依赖 UserSettings）

#### 5.4.2 完全回退时的数据清理（谨慎操作！）

**⚠️ 警告：仅在你确定要彻底回退 SSO + helpdesk 集成时才执行。**

操作前必须先**确保没有工单处于开放状态且负责人为 SSO 用户**，或者先将这些工单重新分配给本地用户。

```bash
# 步骤 1：确认有多少 SSO 用户被分配了开放工单（替换 sso_ 为你的 SSO 用户标识前缀）
python manage.py shell -c "
from helpdesk.models import Ticket
from helpdesk.settings import TICKET_OPEN_STATUSES
open_tickets = Ticket.objects.filter(status__in=TICKET_OPEN_STATUSES, assigned_to__username__startswith='sso_')
print(f'SSO 用户负责的开放工单数: {open_tickets.count()}')
for t in open_tickets:
    print(f'  - {t.ticket} | 负责人: {t.assigned_to.username} | 标题: {t.title}')
"

# 步骤 2：如果上述结果非空，先批量重新分配（示例：全部转给 admin）
python manage.py shell -c "
from django.contrib.auth import get_user_model
from helpdesk.models import Ticket
from helpdesk.settings import TICKET_OPEN_STATUSES
User = get_user_model()
admin = User.objects.get(username='admin')
updated = Ticket.objects.filter(
    status__in=TICKET_OPEN_STATUSES,
    assigned_to__username__startswith='sso_'
).update(assigned_to=admin)
print(f'已重新分配 {updated} 张工单给 admin')
"

# 步骤 3：删除 SSO 用户的 UserSettings（可选，一般不推荐删）
# python manage.py shell -c "
# from helpdesk.models import UserSettings
# deleted, _ = UserSettings.objects.filter(user__username__startswith='sso_').delete()
# print(f'已删除 {deleted} 条 UserSettings')
# "
```

#### 5.4.3 紧急回退快速方案

如果 SSO 刚上线发现问题需要立即止损，最简单的办法是：
1. **关 cron**（注释掉 `create_usersettings`）
2. **改默认值**（在 `settings.py` 中设 `HELPDESK_DEFAULT_SETTINGS` 将两个 email 开关设为 `False`）
3. **批量关已有 SSO 用户的通知**：
```bash
python manage.py shell -c "
from helpdesk.models import UserSettings
updated = UserSettings.objects.filter(
    user__username__startswith='sso_'   # 替换为你的 SSO 用户标识
).update(
    email_on_ticket_change=False,
    email_on_ticket_assign=False
)
print(f'已关闭 {updated} 个 SSO 用户的邮件通知')
"
```
这样历史 UserSettings 保留，不会 500，只是邮件停发，最安全。

### 5.5 上线后冒烟测试清单（必须通过才能放流量）

| # | 测试项 | 操作 | 预期结果 | 对应风险点 |
|---|-------|------|---------|-----------|
| 1 | 新 SSO 用户分配工单不 500 | 用从未登录过的 SSO 账号 → 管理员将其设为某工单负责人 → 添加一条评论 | 评论成功保存，页面不报错，该用户收到分配通知邮件 | §4.4 `RelatedObjectDoesNotExist` 风险 |
| 2 | SSO 用户自己订阅 TicketCC | SSO 用户登录 → 打开一张工单 → 点 Subscribe → 该工单添加一条公开评论 | 用户收到评论通知邮件 | §4.3 TicketCC 判定 |
| 3 | Queue 开关生效 | 选一个 Queue 关 `enable_notifications_on_email_events` → 上述 SSO 用户在该队列工单的订阅 → 添加评论 | 用户**不**收到邮件（验证开关可控） | §5.1.2 Queue 级别开关 |
| 4 | UserSettings 默认值正确 | 新建 SSO 用户 → 查其 `usersettings_helpdesk.email_on_ticket_change` | 值为 `True`（或你在 `HELPDESK_DEFAULT_SETTINGS` 中设的值） | §4.2 默认值来源 |
| 5 | cron 定时补全生效 | 手动往 auth_user 插一个测试用户（绕过 post_save） → 手动跑 `create_usersettings` → 验证该用户有了 UserSettings | 新用户 UserSettings 被创建 | §5.3 cron 补全 |

---

## 六、Monitoring Playbook（Prometheus + Grafana 可直接落地版）

> 本章节提供**可直接复制粘贴**的 Prometheus 告警规则、metrics 导出方法、以及 Grafana 看板 JSON 骨架。所有规则均对应前面章节提到的三类问题：UserSettings 缺失、assigned_to 邮件丢失、ticket_cc 抄送失效。

### 前置条件：安装 django-prometheus

django-helpdesk 源码本身没有内置 metrics 导出（`grep -i "prometheus\|metrics" src/helpdesk/` 无匹配），需要通过 `django-prometheus` 来暴露 Django 层和数据库层 metrics。

**安装**：
```bash
pip install django-prometheus
```

**配置 `settings.py`**：
```python
INSTALLED_APPS = [
    'django_prometheus',
    'helpdesk',
    # ...
]

MIDDLEWARE = [
    'django_prometheus.middleware.PrometheusBeforeMiddleware',
    # ... 其他 middleware ...
    'django_prometheus.middleware.PrometheusAfterMiddleware',
]

# 数据库后端（可选，但推荐，可获取 DB 操作 metrics）
DATABASES = {
    'default': {
        'ENGINE': 'django_prometheus.db.backends.postgresql',  # 或 mysql / sqlite3
        'NAME': 'helpdesk',
        # ...
    }
}
```

**配置 `urls.py`**：
```python
from django_prometheus import exports

urlpatterns = [
    # ...
    path('metrics/', exports.ExportToDjangoView.as_view(), name='prometheus-metrics'),
]
```

---

### 6.1 问题分类一：UserSettings 缺失（导致 `RelatedObjectDoesNotExist` → 500）

**代码路径**：

1. 分配工单 / 评论更新时直接访问反向外键，无 try/except 兜底：
   ```python
   # src/helpdesk/update_ticket.py:164-170
   if ticket.assigned_to and (
       ticket.assigned_to.usersettings_helpdesk.email_on_ticket_change  # ← 不存在抛异常
       or (reassigned and ticket.assigned_to.usersettings_helpdesk.email_on_ticket_assign)
   ):
   ```
   同样的模式出现在 `src/helpdesk/forms.py:425-428`（新工单分配）和 `src/helpdesk/views/staff.py:849-852`（批量关闭）。

2. `post_save` 信号仅在 `created=True` 时触发，`bulk_create` / 原生 SQL / 已存在用户后续 save 均不触发：
   ```python
   # src/helpdesk/models.py:1786-1799
   def create_usersettings(sender, instance, created, **kwargs):
       if created:  # ← 仅新建时
           UserSettings.objects.create(user=instance)
   ```

3. `create_usersettings` 管理命令使用 `get_or_create` 兜底（cron 执行）：
   ```python
   # src/helpdesk/management/commands/create_usersettings.py:30-33
   def handle(self, *args, **options):
       for u in User.objects.all():
           UserSettings.objects.get_or_create(user=u)  # ← 幂等
   ```

#### 6.1.1 监控方案

**方案 A：通过自定义 Django Command 导出 Gauge（推荐，最准确）**

新增 `src/helpdesk/management/commands/export_helpdesk_metrics.py`：
```python
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from helpdesk.models import UserSettings, Ticket, Queue, TicketCC
from prometheus_client import CollectorRegistry, Gauge, write_to_textfile

User = get_user_model()
REGISTRY = CollectorRegistry()

HELPDESK_USERSETTINGS_MISSING = Gauge(
    'helpdesk_usersettings_missing_total',
    'Number of User rows without corresponding UserSettings',
    registry=REGISTRY,
)

HELPDESK_USERSETTINGS_MISSING_ASSIGNED = Gauge(
    'helpdesk_usersettings_missing_assigned_total',
    'Number of Users with assigned open tickets but no UserSettings (HIGH RISK)',
    registry=REGISTRY,
)

HELPDESK_TICKETCC_QUEUE_DISABLED = Gauge(
    'helpdesk_ticketcc_queue_disabled_total',
    'Number of active TicketCC subscriptions whose Queue has enable_notifications_on_email_events=False',
    ['queue_slug'],
    registry=REGISTRY,
)

HELPDESK_EMAIL_SEND_FAILURES = Gauge(
    'helpdesk_email_send_failures_total',
    'Count of SMTP failures captured from helpdesk logs',
    ['role', 'template_name'],
    registry=REGISTRY,
)

class Command(BaseCommand):
    help = 'Export helpdesk metrics to Prometheus .prom file'

    def add_arguments(self, parser):
        parser.add_argument('output_path', type=str, help='Path to write .prom file')

    def handle(self, *args, **options):
        missing = User.objects.exclude(
            pk__in=UserSettings.objects.values_list('user_id', flat=True)
        ).count()
        HELPDESK_USERSETTINGS_MISSING.set(missing)

        from helpdesk.settings import TICKET_OPEN_STATUSES
        missing_assigned = User.objects.filter(
            assigned_to__status__in=TICKET_OPEN_STATUSES
        ).exclude(
            pk__in=UserSettings.objects.values_list('user_id', flat=True)
        ).distinct().count()
        HELPDESK_USERSETTINGS_MISSING_ASSIGNED.set(missing_assigned)

        for q in Queue.objects.all():
            count = TicketCC.objects.filter(
                ticket__queue=q,
                ticket__status__in=TICKET_OPEN_STATUSES,
            ).count() if not q.enable_notifications_on_email_events else 0
            HELPDESK_TICKETCC_QUEUE_DISABLED.labels(queue_slug=q.slug).set(count)

        write_to_textfile(options['output_path'], REGISTRY)
        self.stdout.write(self.style.SUCCESS('Metrics exported successfully'))
```

**配合 cron 定时导出 + node_exporter textfile collector**：
```cron
# 每 5 分钟导出一次
*/5 * * * * cd /path/to/project && /path/to/venv/bin/python manage.py export_helpdesk_metrics /var/lib/node_exporter/helpdesk.prom
```

**方案 B：无代码侵入，仅用 django-prometheus 自带 metrics + 日志监控**

如果不想新增代码，可以通过以下组合间接监控：

1. **HTTP 500 异常率监控**（`django_http_responses_total_by_status_total{status="500"}`）
2. **日志关键字告警**（`RelatedObjectDoesNotExist` / `UserSettings`）

---

#### 6.1.2 Prometheus 告警规则（可直接复制）

```yaml
groups:
- name: helpdesk_usersettings
  rules:

  # 告警级别：CRITICAL — 有开放工单的负责人缺失 UserSettings，下一次评论/更新必 500
  - alert: HelpdeskUserSettingsMissingForAssigned
    expr: helpdesk_usersettings_missing_assigned_total > 0
    for: 1m
    labels:
      severity: critical
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "High risk: {{ $value }} User(s) with open assigned tickets have NO UserSettings"
      description: "These users are assigned to at least one open ticket but lack UserSettings. Any follow-up / reassign on those tickets will trigger HTTP 500. Run `python manage.py create_usersettings` immediately. Source: src/helpdesk/update_ticket.py:164-170"
      runbook: "docs/notification-recipients.md#612-prometheus-"

  # 告警级别：WARNING — 存在缺失 UserSettings 的用户（暂时未分配工单，但风险存在）
  - alert: HelpdeskUserSettingsMissing
    expr: helpdesk_usersettings_missing_total > 0
    for: 5m
    labels:
      severity: warning
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "{{ $value }} User(s) are missing UserSettings"
      description: "These users were likely synced via LDAP/SSO bypassing post_save signal (src/helpdesk/models.py:1786-1799). Run `python manage.py create_usersettings` (src/helpdesk/management/commands/create_usersettings.py:30-33) or ensure cron job is running."
      runbook: "docs/notification-recipients.md#52-当天首次补全放流量前必须执行"

  # 告警级别：WARNING — create_usersettings cron 未在预期时间执行（每天 >24h 未执行）
  - alert: HelpdeskCreateUserSettingsCronStale
    expr: time() - helpdesk_usersettings_missing_timestamp_seconds > 86400
    for: 10m
    labels:
      severity: warning
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "create_usersettings cron job hasn't run for more than 24h"
      description: "Check cron status at /etc/cron.d/helpdesk. Expected: 0 3 * * * (src/helpdesk/management/commands/create_usersettings.py:30-33)"
```

> **注**：`helpdesk_usersettings_missing_timestamp_seconds` 是在 export 命令末尾追加的时间戳 gauge，建议加入 export 脚本：
> ```python
> HELPDESK_EXPORT_TIMESTAMP = Gauge('helpdesk_usersettings_missing_timestamp_seconds', 'Last export time', registry=REGISTRY)
> HELPDESK_EXPORT_TIMESTAMP.set(time.time())
> ```

---

### 6.2 问题分类二：assigned_to 邮件丢失（用户收不到分配/变更通知）

**代码路径**：

`send_templated_mail` 是所有邮件的统一出口，定义于 `src/helpdesk/templated_email.py:11-135`。

关键调用链（assigned_to 通知）：
1. 各场景判断是否发送（基于 `usersettings_helpdesk.email_on_ticket_change/assign`）→ `src/helpdesk/update_ticket.py:164-170`
2. 调用 `ticket.send()` → `src/helpdesk/models.py:638-694`
3. 内部 `send(role, recipient)` 闭包调用 `send_templated_mail` → `src/helpdesk/models.py:674-684`
4. `send_templated_mail` 捕获 `SMTPException`，记录 `logger.exception()`，根据 `fail_silently` 决定是否向上抛 → `src/helpdesk/templated_email.py:127-134`

**注意**：批量关闭和工单合并场景传入了 `fail_silently=True`（`src/helpdesk/views/staff.py:858`, `src/helpdesk/views/staff.py:985`），此时 SMTP 失败不会抛 500，但**仍然会写 `logger.exception` 日志**。

**用户侧静默失败原因**：
- UserSettings 的 `email_on_ticket_change=False` 或 `email_on_ticket_assign=False`（代码根本不会调用 `send_templated_mail`）
- SMTP 服务器拒绝（被 logger.exception 捕获）
- 邮件模板不存在（`EmailTemplate.DoesNotExist`）→ 写 `logger.warning`，`return` 不发送（`src/helpdesk/templated_email.py:65-76`）

#### 6.2.1 监控方案

**基于日志 + promtail 采集**（推荐，最准确）：

`promtail-config.yaml` 关键配置：
```yaml
scrape_configs:
- job_name: helpdesk_app_logs
  static_configs:
  - targets:
      - localhost
    labels:
      job: helpdesk_app_logs
      __path__: /var/log/helpdesk/*.log

  pipeline_stages:
  # 匹配 SMTPException 日志
  - match:
      selector: '{job="helpdesk_app_logs"}'
      stages:
      - regex:
          expression: 'SMTPException raised while sending email to (?P<recipient>\S+)'
      - labels:
          recipient:
      - match:
          selector: '{job="helpdesk_app_logs", recipient!=""}'
          stages:
          - metrics:
              helpdesk_smtp_failure_total:
                type: Counter
                description: "Total SMTP failures when sending helpdesk emails"
                config:
                  match_all: true
                  action: inc

  # 匹配模板不存在日志
  - match:
      selector: '{job="helpdesk_app_logs"}'
      stages:
      - regex:
          expression: 'template "(?P<template_name>\S+)" does not exist'
      - labels:
          template_name:
      - match:
          selector: '{job="helpdesk_app_logs", template_name!=""}'
          stages:
          - metrics:
              helpdesk_template_missing_total:
                type: Counter
                description: "Total missing template incidents"
                config:
                  match_all: true
                  action: inc
```

#### 6.2.2 Prometheus 告警规则

```yaml
groups:
- name: helpdesk_assigned_to_email
  rules:

  # 告警级别：WARNING — 最近 5 分钟内 SMTP 发送失败 > 0
  - alert: HelpdeskSMTPFailures
    expr: increase(helpdesk_smtp_failure_total[5m]) > 0
    for: 1m
    labels:
      severity: warning
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "{{ $value | humanize }} SMTP failure(s) in last 5 minutes"
      description: "Failed to send email to recipients. Check SMTP server status and helpdesk logs. Source: src/helpdesk/templated_email.py:127-134 (SMTPException handler)"
      affected_role: "assigned_to (src/helpdesk/update_ticket.py:164-170 calls ticket.send → src/helpdesk/models.py:674-684 → send_templated_mail)"

  # 告警级别：CRITICAL — 邮件模板缺失（该场景所有邮件都发不出）
  - alert: HelpdeskEmailTemplateMissing
    expr: increase(helpdesk_template_missing_total[5m]) > 0
    for: 1m
    labels:
      severity: critical
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "Email template '{{ $labels.template_name }}' is missing!"
      description: "All emails using this template will be silently dropped. Source: src/helpdesk/templated_email.py:65-76 (EmailTemplate.DoesNotExist handler)"
      runbook: "Go to Django Admin → EmailTemplate and create template '{{ $labels.template_name }}' for the required locale"

  # 告警级别：INFO — assigned_to 角色通知比率下降（可选，需配合自定义 metrics）
  # 如果你在代码中加入了发送计数 Counter（如下），可以用该规则监控静默失败：
  #
  #   # 在 send_templated_mail 开头加：
  #   from prometheus_client import Counter
  #   EMAIL_SENT = Counter('helpdesk_email_sent_total', 'Emails sent', ['role', 'template_name', 'status'])
  #   # 成功发送后: EMAIL_SENT.labels(role=context.get('role', 'unknown'), template_name=template_name, status='success').inc()
  #   # 失败后: EMAIL_SENT.labels(role=context.get('role', 'unknown'), template_name=template_name, status='failure').inc()
  #
  - alert: HelpdeskAssignedToEmailDrop
    expr: |
      rate(helpdesk_email_sent_total{role="assigned_to",status="success"}[1h])
      /
      (rate(helpdesk_email_sent_total{role="assigned_to",status="success"}[1h]) + rate(helpdesk_email_sent_total{role="assigned_to",status="failure"}[1h]))
      < 0.8
    for: 15m
    labels:
      severity: warning
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "Assigned_to email delivery ratio dropped below 80%"
      description: "Check if assigned_to users have email_on_ticket_change=False (src/helpdesk/update_ticket.py:164-170 condition) or SMTP issues"
```

---

### 6.3 问题分类三：ticket_cc 抄送失效（订阅者收不到通知）

**代码路径**：

`Ticket.send()` 中决定是否遍历 `ticketcc_set` 的条件（`src/helpdesk/models.py:691-693`）：
```python
# src/helpdesk/models.py:691-693
if self.queue.enable_notifications_on_email_events:
    for cc in self.ticketcc_set.select_related("user").all():
        send("ticket_cc", cc.email_address)  # ← 仅当 Queue 开关打开时才会遍历
```

`TicketCC.email_address` 的解析（`src/helpdesk/models.py:1946-1952`）：
```python
# src/helpdesk/models.py:1946-1952
def _email_address(self):
    if self.user and self.user.email is not None:
        return self.user.email    # ← SSO 外部用户走这里
    else:
        return self.email          # ← 裸邮箱订阅走这里
email_address = property(_email_address)
```

**抄送失效根因**：
1. `queue.enable_notifications_on_email_events=False` → 整支循环被跳过（最常见）
2. `TicketCC.user` 为 SSO 用户但该用户的 `user.email` 为空 → 返回 `None` → `should_receive(None)` 返回 False（`src/helpdesk/models.py:671-672`）
3. 调用方 `roles` 字典未包含 `ticket_cc` key → `send(role, recipient)` 闭包中的 `role in roles` 条件不满足（`src/helpdesk/models.py:675`）
4. Queue 级 `updated_ticket_cc` 为空（该地址始终会被尝试发送，但空值被 `should_receive` 过滤）

#### 6.3.1 监控方案

**基于自定义 metrics（§6.1.1 的 `export_helpdesk_metrics` 命令已有）**：
- `helpdesk_ticketcc_queue_disabled_total{queue_slug="..."}`：每个 Queue 下有多少活跃 TicketCC 订阅但 Queue 开关为 False

**可选：在 `Ticket.send()` 中加入计数 Counter**（如果你愿意改代码，最精确）：

在 `src/helpdesk/models.py:638` 之前添加：
```python
from prometheus_client import Counter

TICKET_CC_ATTEMPTS = Counter(
    'helpdesk_ticket_cc_attempts_total',
    'Number of ticket_cc notification attempts',
    ['queue_slug', 'enabled', 'role_present', 'recipient_valid'],
)
```

然后在 `send("ticket_cc", ...)` 调用前后（`src/helpdesk/models.py:687`, `src/helpdesk/models.py:691-693`）埋点：
```python
# 发送 queue.updated_ticket_cc 前
cc_enabled = self.queue.enable_notifications_on_email_events
role_present = "ticket_cc" in roles
recipient_valid = should_receive(self.queue.updated_ticket_cc)
TICKET_CC_ATTEMPTS.labels(
    queue_slug=self.queue.slug,
    enabled=str(cc_enabled),
    role_present=str(role_present),
    recipient_valid=str(recipient_valid),
).inc()
send("ticket_cc", self.queue.updated_ticket_cc)

# 遍历 ticketcc_set 前
if cc_enabled:
    for cc in self.ticketcc_set.select_related("user").all():
        cc_addr = cc.email_address
        recipient_valid = should_receive(cc_addr)
        TICKET_CC_ATTEMPTS.labels(
            queue_slug=self.queue.slug,
            enabled=str(cc_enabled),
            role_present=str(role_present),
            recipient_valid=str(recipient_valid),
        ).inc()
        send("ticket_cc", cc_addr)
```

#### 6.3.2 Prometheus 告警规则

```yaml
groups:
- name: helpdesk_ticket_cc
  rules:

  # 告警级别：WARNING — Queue 开关为 False 但有活跃订阅者（订阅全失效）
  - alert: HelpdeskTicketCCQueueDisabled
    expr: helpdesk_ticketcc_queue_disabled_total > 0
    for: 5m
    labels:
      severity: warning
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "Queue '{{ $labels.queue_slug }}' has {{ $value }} active TicketCC subscriptions but enable_notifications_on_email_events=False"
      description: "All ticket_cc notifications for this Queue are skipped. Source: src/helpdesk/models.py:691-693 (if condition gate)"
      runbook: "docs/notification-recipients.md#512-queue-级别-enable_notifications_on_email_events-核对"
      fix_command: "python manage.py shell -c \"from helpdesk.models import Queue; Queue.objects.filter(slug='{{ $labels.queue_slug }}').update(enable_notifications_on_email_events=True)\""

  # 告警级别：WARNING — ticket_cc 发送成功率下降（需要埋点 Counter）
  - alert: HelpdeskTicketCCDeliveryDrop
    expr: |
      rate(helpdesk_ticket_cc_attempts_total{recipient_valid="True"}[1h])
      /
      rate(helpdesk_ticket_cc_attempts_total[1h])
      < 0.5
    for: 15m
    labels:
      severity: warning
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "ticket_cc delivery ratio below 50% on queue {{ $labels.queue_slug }}"
      description: "role_present={{ $labels.role_present }}, enabled={{ $labels.enabled }}. Check if TicketCC.user.email is empty (src/helpdesk/models.py:1946-1952) or roles dict missing 'ticket_cc' key (src/helpdesk/models.py:675)"

  # 告警级别：WARNING — SSO 用户订阅但 email 为空
  # (需要额外埋点，或在 export 命令中加入)
  - alert: HelpdeskTicketCCSSOUserEmailEmpty
    expr: helpdesk_ticketcc_sso_email_empty_total > 0
    for: 5m
    labels:
      severity: warning
      team: sre
      category: helpdesk_notification
    annotations:
      summary: "{{ $value }} TicketCC subscription(s) have SSO user with empty email"
      description: "TicketCC references a user (likely SSO-synced) whose user.email is None. These subscriptions will silently fail. Source: src/helpdesk/models.py:1946-1952 (_email_address property)"
```

---

### 6.4 Grafana 看板 JSON 骨架（仅必填字段，可直接导入）

将以下 JSON 保存为 `helpdesk-notification-dashboard.json`，在 Grafana → Dashboards → Import 中导入即可。

```json
{
  "annotations": {
    "list": []
  },
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 0,
  "id": null,
  "links": [],
  "liveNow": false,
  "panels": [
    {
      "datasource": {
        "type": "prometheus",
        "uid": "prometheus"
      },
      "fieldConfig": {
        "defaults": {
          "color": {
            "mode": "thresholds"
          },
          "mappings": [],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {
                "color": "green",
                "value": null
              },
              {
                "color": "red",
                "value": 0
              }
            ]
          },
          "unit": "short"
        },
        "overrides": []
      },
      "gridPos": {
        "h": 4,
        "w": 12,
        "x": 0,
        "y": 0
      },
      "id": 1,
      "options": {
        "colorMode": "value",
        "graphMode": "area",
        "justifyMode": "auto",
        "orientation": "auto",
        "reduceOptions": {
          "calcs": [
            "lastNotNull"
          ],
          "fields": "",
          "values": false
        },
        "textMode": "auto"
      },
      "pluginVersion": "10.4.0",
      "targets": [
        {
          "expr": "helpdesk_usersettings_missing_assigned_total",
          "refId": "A",
          "datasource": {
            "type": "prometheus",
            "uid": "prometheus"
          }
        }
      ],
      "title": "⚠️ HIGH RISK: Users with open tickets but NO UserSettings",
      "type": "stat",
      "description": "src/helpdesk/update_ticket.py:164-170 will throw RelatedObjectDoesNotExist on next update"
    },
    {
      "datasource": {
        "type": "prometheus",
        "uid": "prometheus"
      },
      "fieldConfig": {
        "defaults": {
          "color": {
            "mode": "thresholds"
          },
          "mappings": [],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {
                "color": "green",
                "value": null
              },
              {
                "color": "yellow",
                "value": 0
              }
            ]
          },
          "unit": "short"
        },
        "overrides": []
      },
      "gridPos": {
        "h": 4,
        "w": 12,
        "x": 12,
        "y": 0
      },
      "id": 2,
      "options": {
        "colorMode": "value",
        "graphMode": "area",
        "justifyMode": "auto",
        "orientation": "auto",
        "reduceOptions": {
          "calcs": [
            "lastNotNull"
          ],
          "fields": "",
          "values": false
        },
        "textMode": "auto"
      },
      "pluginVersion": "10.4.0",
      "targets": [
        {
          "expr": "helpdesk_usersettings_missing_total",
          "refId": "A",
          "datasource": {
            "type": "prometheus",
            "uid": "prometheus"
          }
        }
      ],
      "title": "Total Users missing UserSettings",
      "type": "stat",
      "description": "These users were synced bypassing post_save (src/helpdesk/models.py:1786-1799)"
    },
    {
      "datasource": {
        "type": "prometheus",
        "uid": "prometheus"
      },
      "fieldConfig": {
        "defaults": {
          "color": {
            "mode": "palette-classic"
          },
          "custom": {
            "axisCenteredZero": false,
            "axisColorMode": "text",
            "axisLabel": "",
            "axisPlacement": "auto",
            "barAlignment": 0,
            "drawStyle": "line",
            "fillOpacity": 10,
            "gradientMode": "none",
            "hideFrom": {
              "legend": false,
              "tooltip": false,
              "viz": false
            },
            "lineInterpolation": "linear",
            "lineWidth": 1,
            "pointSize": 5,
            "scaleDistribution": {
              "type": "linear"
            },
            "showPoints": "auto",
            "spanNulls": false,
            "stacking": {
              "group": "A",
              "mode": "none"
            },
            "thresholdsStyle": {
              "mode": "off"
            }
          },
          "mappings": [],
          "min": 0,
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {
                "color": "green",
                "value": null
              }
            ]
          },
          "unit": "short"
        },
        "overrides": []
      },
      "gridPos": {
        "h": 8,
        "w": 24,
        "x": 0,
        "y": 4
      },
      "id": 3,
      "options": {
        "legend": {
          "calcs": [],
          "displayMode": "list",
          "placement": "bottom",
          "showLegend": true
        },
        "tooltip": {
          "mode": "multi",
          "sort": "none"
        }
      },
      "targets": [
        {
          "expr": "sum by (role) (rate(helpdesk_email_sent_total{status=\"success\"}[5m]))",
          "legendFormat": "{{role}} - success",
          "refId": "A",
          "datasource": {
            "type": "prometheus",
            "uid": "prometheus"
          }
        },
        {
          "expr": "sum by (role) (rate(helpdesk_email_sent_total{status=\"failure\"}[5m]))",
          "legendFormat": "{{role}} - failure",
          "refId": "B",
          "datasource": {
            "type": "prometheus",
            "uid": "prometheus"
          }
        },
        {
          "expr": "sum by (role) (rate(helpdesk_smtp_failure_total[5m]))",
          "legendFormat": "SMTP exceptions",
          "refId": "C",
          "datasource": {
            "type": "prometheus",
            "uid": "prometheus"
          }
        }
      ],
      "title": "Email Delivery Metrics (5m rate)",
      "type": "timeseries",
      "description": "Source: send_templated_mail in src/helpdesk/templated_email.py:11-135"
    },
    {
      "datasource": {
        "type": "prometheus",
        "uid": "prometheus"
      },
      "fieldConfig": {
        "defaults": {
          "color": {
            "mode": "thresholds"
          },
          "mappings": [],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {
                "color": "green",
                "value": null
              },
              {
                "color": "yellow",
                "value": 0
              }
            ]
          },
          "unit": "short"
        },
        "overrides": []
      },
      "gridPos": {
        "h": 4,
        "w": 12,
        "x": 0,
        "y": 12
      },
      "id": 4,
      "options": {
        "colorMode": "value",
        "graphMode": "none",
        "justifyMode": "auto",
        "orientation": "auto",
        "reduceOptions": {
          "calcs": [
            "lastNotNull"
          ],
          "fields": "",
          "values": false
        },
        "textMode": "auto"
      },
      "pluginVersion": "10.4.0",
      "targets": [
        {
          "expr": "sum(helpdesk_ticketcc_queue_disabled_total)",
          "refId": "A",
          "datasource": {
            "type": "prometheus",
            "uid": "prometheus"
          }
        }
      ],
      "title": "TicketCC subscriptions on disabled Queues",
      "type": "stat",
      "description": "These subscriptions receive no notifications. src/helpdesk/models.py:691-693"
    },
    {
      "datasource": {
        "type": "prometheus",
        "uid": "prometheus"
      },
      "fieldConfig": {
        "defaults": {
          "color": {
            "mode": "thresholds"
          },
          "mappings": [],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {
                "color": "green",
                "value": null
              },
              {
                "color": "red",
                "value": 0
              }
            ]
          },
          "unit": "s"
        },
        "overrides": []
      },
      "gridPos": {
        "h": 4,
        "w": 12,
        "x": 12,
        "y": 12
      },
      "id": 5,
      "options": {
        "colorMode": "value",
        "graphMode": "area",
        "justifyMode": "auto",
        "orientation": "auto",
        "reduceOptions": {
          "calcs": [
            "lastNotNull"
          ],
          "fields": "",
          "values": false
        },
        "textMode": "auto"
      },
      "pluginVersion": "10.4.0",
      "targets": [
        {
          "expr": "time() - helpdesk_usersettings_missing_timestamp_seconds",
          "refId": "A",
          "datasource": {
            "type": "prometheus",
            "uid": "prometheus"
          }
        }
      ],
      "title": "Seconds since last create_usersettings run",
      "type": "stat",
      "description": "Should be < 86400 (24h). cron: src/helpdesk/management/commands/create_usersettings.py:30-33"
    },
    {
      "datasource": {
        "type": "prometheus",
        "uid": "prometheus"
      },
      "fieldConfig": {
        "defaults": {
          "color": {
            "mode": "palette-classic"
          },
          "custom": {
            "axisCenteredZero": false,
            "axisColorMode": "text",
            "axisLabel": "",
            "axisPlacement": "auto",
            "barAlignment": 0,
            "drawStyle": "line",
            "fillOpacity": 10,
            "gradientMode": "none",
            "hideFrom": {
              "legend": false,
              "tooltip": false,
              "viz": false
            },
            "lineInterpolation": "linear",
            "lineWidth": 1,
            "pointSize": 5,
            "scaleDistribution": {
              "type": "linear"
            },
            "showPoints": "auto",
            "spanNulls": false,
            "stacking": {
              "group": "A",
              "mode": "none"
            },
            "thresholdsStyle": {
              "mode": "off"
            }
          },
          "mappings": [],
          "min": 0,
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {
                "color": "green",
                "value": null
              }
            ]
          },
          "unit": "short"
        },
        "overrides": []
      },
      "gridPos": {
        "h": 8,
        "w": 24,
        "x": 0,
        "y": 16
      },
      "id": 6,
      "options": {
        "legend": {
          "calcs": [],
          "displayMode": "list",
          "placement": "bottom",
          "showLegend": true
        },
        "tooltip": {
          "mode": "multi",
          "sort": "none"
        }
      },
      "targets": [
        {
          "expr": "sum by (queue_slug, enabled) (rate(helpdesk_ticket_cc_attempts_total[5m]))",
          "legendFormat": "queue={{queue_slug}} enabled={{enabled}}",
          "refId": "A",
          "datasource": {
            "type": "prometheus",
            "uid": "prometheus"
          }
        }
      ],
      "title": "TicketCC Notification Attempts (5m rate)",
      "type": "timeseries",
      "description": "Source: src/helpdesk/models.py:687 and src/helpdesk/models.py:691-693"
    }
  ],
  "refresh": "30s",
  "schemaVersion": 39,
  "tags": [
    "helpdesk",
    "notification",
    "sso"
  ],
  "templating": {
    "list": [
      {
        "current": {
          "selected": false,
          "text": "prometheus",
          "value": "prometheus"
        },
        "hide": 0,
        "includeAll": false,
        "label": "Data Source",
        "multi": false,
        "name": "datasource",
        "options": [],
        "query": "prometheus",
        "queryValue": "",
        "refresh": 1,
        "regex": "",
        "skipUrlSync": false,
        "type": "datasource"
      }
    ]
  },
  "time": {
    "from": "now-24h",
    "to": "now"
  },
  "timepicker": {},
  "timezone": "browser",
  "title": "Helpdesk Notification Health",
  "uid": "helpdesk-notification-health",
  "version": 1,
  "weekStart": ""
}
```

**看板包含的面板**：
1. **HIGH RISK: Users with open tickets but NO UserSettings**（Stat 面板，红色阈值）→ CRITICAL 级
2. **Total Users missing UserSettings**（Stat 面板，黄色阈值）→ WARNING 级
3. **Email Delivery Metrics (5m rate)**（TimeSeries，按 role 区分成功/失败/SMTP 异常）
4. **TicketCC subscriptions on disabled Queues**（Stat，统计被 Queue 开关屏蔽的订阅数）
5. **Seconds since last create_usersettings run**（Stat，监控 cron 新鲜度，>24h 告警）
6. **TicketCC Notification Attempts (5m rate)**（TimeSeries，按 Queue + enabled 状态拆分）

**导入后需要修改的项**（均在看板变量 `templating.list[0]` 中）：
- `datasource.uid`：替换为你的 Grafana 中 Prometheus 数据源的实际 UID
- 各 panel 的 `datasource.uid`：同上

---

### 6.5 三类监控问题速查表

| 问题 | 根因代码位置 | 监控方式 | PromQL 关键字段 | 告警级别 |
|------|-------------|---------|----------------|---------|
| **UserSettings 缺失 + 持开放工单** | `src/helpdesk/update_ticket.py:164-170`（无兜底直接访问反向外键） | 自定义 Command + node_exporter textfile | `helpdesk_usersettings_missing_assigned_total > 0` | **CRITICAL** |
| **UserSettings 缺失（暂无工单）** | `src/helpdesk/models.py:1786-1799`（post_save 仅 created=True） | 同上 | `helpdesk_usersettings_missing_total > 0` | WARNING |
| **cron 未执行** | `src/helpdesk/management/commands/create_usersettings.py:30-33`（get_or_create 兜底） | 同上 + 时间戳 gauge | `time() - helpdesk_usersettings_missing_timestamp_seconds > 86400` | WARNING |
| **SMTP 发送失败** | `src/helpdesk/templated_email.py:127-134`（SMTPException 捕获） | promtail 日志解析 | `increase(helpdesk_smtp_failure_total[5m]) > 0` | WARNING |
| **邮件模板缺失** | `src/helpdesk/templated_email.py:65-76`（EmailTemplate.DoesNotExist） | promtail 日志解析 | `increase(helpdesk_template_missing_total[5m]) > 0` | **CRITICAL** |
| **Queue 关闭但有活跃订阅** | `src/helpdesk/models.py:691-693`（if 开关判断） | 自定义 Command | `helpdesk_ticketcc_queue_disabled_total > 0` | WARNING |
| **SSO 用户订阅但 email 为空** | `src/helpdesk/models.py:1946-1952`（email_address 属性） | 自定义 Command + 埋点 | `helpdesk_ticketcc_sso_email_empty_total > 0` | WARNING |

---

## 七、helpdesk 端埋点改造指引（让 §6 的 Prometheus 表达式真正有 series）

> §6 的 Prometheus 告警表达式需要 helpdesk 端有对应的 metrics 导出才能生效。django-helpdesk 源码**没有内置任何 Prometheus 埋点**（`src/helpdesk/` 全目录无 `prometheus` / `Counter` / `Gauge` 引用），因此需要手动改造。本章逐一列出每条 PromQL 表达式对应的代码改造位置、指标类型、依赖配置和改造后的完整代码。

### 7.0 前置依赖

```bash
pip install prometheus-client django-prometheus
```

在 `settings.py` 中：
```python
INSTALLED_APPS = [
    'django_prometheus',
    'helpdesk',
    # ...
]

MIDDLEWARE = [
    'django_prometheus.middleware.PrometheusBeforeMiddleware',
    # ... 其他 middleware ...
    'django_prometheus.middleware.PrometheusAfterMiddleware',
]
```

在 `urls.py` 中暴露 metrics 端点：
```python
from django_prometheus import exports
urlpatterns = [
    path('metrics/', exports.ExportToDjangoView.as_view(), name='prometheus-metrics'),
]
```

新增配置项（可选，默认为 `True` 即开启所有埋点）：
```python
# settings.py
HELPDESK_METRICS_ENABLED = getattr(settings, 'HELPDESK_METRICS_ENABLED', True)
```

---

### 7.1 埋点改造 1：`send_templated_mail` 邮件发送结果

**对应 PromQL 表达式**：
- `helpdesk_email_sent_total{status="success"}` / `helpdesk_email_sent_total{status="failure"}`
- `helpdesk_email_sent_total{status="template_missing"}`
- `helpdesk_smtp_failure_total`（§6.2.2 `HelpdeskSMTPFailures`）
- `helpdesk_template_missing_total`（§6.2.2 `HelpdeskEmailTemplateMissing`）
- `helpdesk_email_sent_total{role="assigned_to"}`（§6.2.2 `HelpdeskAssignedToEmailDrop`）

**指标类型**：`Counter`（单调递增，适合 `rate()` / `increase()` 运算）

**埋点文件**：`src/helpdesk/templated_email.py`

**改造后的完整代码**：

```python
# src/helpdesk/templated_email.py — 完整替换
from django.conf import settings
from django.utils.safestring import mark_safe
import logging
import os
from smtplib import SMTPException

from prometheus_client import Counter

logger = logging.getLogger("helpdesk")

HELPDESK_METRICS_ENABLED = getattr(
    settings, "HELPDESK_METRICS_ENABLED", True
)

HELPDESK_EMAIL_SENT = Counter(
    "helpdesk_email_sent_total",
    "Total helpdesk notification emails attempted",
    ["role", "template_name", "status"],
)

HELPDESK_SMTP_FAILURE = Counter(
    "helpdesk_smtp_failure_total",
    "Total SMTP failures in helpdesk email sending",
    ["template_name"],
)

HELPDESK_TEMPLATE_MISSING = Counter(
    "helpdesk_template_missing_total",
    "Total missing email template incidents",
    ["template_name"],
)


def _inc_counter(counter, **labels):
    if HELPDESK_METRICS_ENABLED:
        counter.labels(**labels).inc()


def send_templated_mail(
    template_name,
    context,
    recipients,
    sender=None,
    bcc=None,
    fail_silently=False,
    files=None,
    extra_headers=None,
):
    """
    send_templated_mail() is a wrapper around Django's e-mail routines that
    allows us to easily send multipart (text/plain & text/html) e-mails using
    templates that are stored in the database. This lets the admin provide
    both a text and a HTML template for each message.

    template_name is the slug of the template to use for this message (see
        models.EmailTemplate)

    context is a dictionary to be used when rendering the template

    recipients can be either a string, eg 'a@b.com', or a list of strings.

    sender should contain a string, eg 'My Site <me@z.com>'. If you leave it
        blank, it'll use settings.DEFAULT_FROM_EMAIL as a fallback.

    bcc is an optional list of addresses that will receive this message as a
        blind carbon copy.

    fail_silently is passed to Django's mail routine. Set to 'True' to ignore
        any errors at send time.

    files can be a list of tuples. Each tuple should be a filename to attach,
        along with the File objects to be read. files can be blank.

    extra_headers is a dictionary of extra email headers, needed to process
        email replies and keep proper threading.

    """
    from django.core.mail import EmailMultiAlternatives
    from django.template import engines

    from_string = engines["django"].from_string

    from helpdesk.models import EmailTemplate
    from helpdesk.settings import (
        HELPDESK_EMAIL_FALLBACK_LOCALE,
        HELPDESK_EMAIL_SUBJECT_TEMPLATE,
    )

    role = context.get("role", "unknown")

    headers = extra_headers or {}

    locale = context["queue"].get("locale") or HELPDESK_EMAIL_FALLBACK_LOCALE

    try:
        t = EmailTemplate.objects.get(
            template_name__iexact=template_name, locale=locale
        )
    except EmailTemplate.DoesNotExist:
        try:
            t = EmailTemplate.objects.get(
                template_name__iexact=template_name, locale__isnull=True
            )
        except EmailTemplate.DoesNotExist:
            logger.warning('template "%s" does not exist, no mail sent', template_name)
            # ← 埋点：模板缺失
            _inc_counter(
                HELPDESK_EMAIL_SENT,
                role=role, template_name=template_name, status="template_missing",
            )
            _inc_counter(
                HELPDESK_TEMPLATE_MISSING,
                template_name=template_name,
            )
            return  # just ignore if template doesn't exist

    subject_part = (
        from_string(HELPDESK_EMAIL_SUBJECT_TEMPLATE % {"subject": t.subject})
        .render(context)
        .replace("\n", "")
        .replace("\r", "")
    )

    footer_file = os.path.join("helpdesk", locale, "email_text_footer.txt")

    text_part = from_string(
        "%s\n\n{%% include '%s' %%}" % (t.plain_text, footer_file)
    ).render(context)

    email_html_base_file = os.path.join("helpdesk", locale, "email_html_base.html")
    # keep new lines in html emails
    if "comment" in context:
        context["comment"] = mark_safe(context["comment"].replace("\r\n", "<br>"))

    html_part = from_string(
        "{%% extends '%s' %%}"
        "{%% block title %%}%s{%% endblock %%}"
        "{%% block content %%}%s{%% endblock %%}"
        % (email_html_base_file, t.heading, t.html)
    ).render(context)

    if isinstance(recipients, str):
        if recipients.find(","):
            recipients = recipients.split(",")
    elif type(recipients) is not list:
        recipients = [recipients]

    msg = EmailMultiAlternatives(
        subject_part,
        text_part,
        sender or settings.DEFAULT_FROM_EMAIL,
        recipients,
        bcc=bcc,
        headers=headers,
    )
    msg.attach_alternative(html_part, "text/html")

    if files:
        for filename, filefield in files:
            filefield.open("rb")
            content = filefield.read()
            msg.attach(filename, content)
            filefield.close()
    logger.debug("Sending email to: {!r}".format(recipients))

    try:
        result = msg.send()
        # ← 埋点：发送成功
        _inc_counter(
            HELPDESK_EMAIL_SENT,
            role=role, template_name=template_name, status="success",
        )
        return result
    except SMTPException as e:
        logger.exception(
            "SMTPException raised while sending email to {}".format(recipients)
        )
        # ← 埋点：SMTP 失败
        _inc_counter(
            HELPDESK_EMAIL_SENT,
            role=role, template_name=template_name, status="failure",
        )
        _inc_counter(
            HELPDESK_SMTP_FAILURE,
            template_name=template_name,
        )
        if not fail_silently:
            raise e
        return 0
```

**改造要点**：
1. 文件顶部新增 3 个 `Counter` 定义：`HELPDESK_EMAIL_SENT`（3 个 label：role / template_name / status）、`HELPDESK_SMTP_FAILURE`（1 个 label：template_name）、`HELPDESK_TEMPLATE_MISSING`（1 个 label：template_name）
2. 从 `context` 字典中取 `role`（由 `Ticket.send()` 传入，见 7.2），默认 `"unknown"`
3. 原有两个 `return` 点前各插入 `_inc_counter`：模板缺失（第 76 行原 `return`）、发送成功（第 128 行原 `return msg.send()`）、SMTP 异常（第 129 行原 `except`）
4. 新增 `HELPDESK_METRICS_ENABLED` settings 开关，可在不卸载 prometheus_client 的情况下通过配置关闭埋点

**关键约束**：`context` 字典必须包含 `"role"` key。当前 `Ticket.send()` 中的 `send(role, recipient)` 闭包（`src/helpdesk/models.py:674-684`）调用 `send_templated_mail` 时**不传 role**，需要改造 `Ticket.send()`（见 7.2）。

---

### 7.2 埋点改造 2：`Ticket.send()` 传递 role 到 `send_templated_mail`

**对应 PromQL 表达式**：通过 `helpdesk_email_sent_total{role="assigned_to"}` 等实现按角色分维度的监控。

**指标类型**：本改造不新增指标，而是在 `send_templated_mail` 调用前向 `context` 注入 `role`，使 7.1 的 `HELPDESK_EMAIL_SENT` Counter 能正确按 role 分类。

**埋点文件**：`src/helpdesk/models.py`

**改造位置**：`Ticket.send()` 内的 `send(role, recipient)` 闭包（第 674-684 行）

**原始代码**（`src/helpdesk/models.py:674-684`）：
```python
def send(role, recipient):
    if recipient and recipient not in recipients and role in roles:
        template, context = roles[role]
        send_templated_mail(
            template,
            context,
            recipient,
            sender=self.queue.from_address,
            **kwargs,
        )
        recipients.add(recipient)
```

**改造后**：
```python
def send(role, recipient):
    if recipient and recipient not in recipients and role in roles:
        template, context = roles[role]
        context["role"] = role  # ← 注入 role，供 send_templated_mail 的 Counter 使用
        send_templated_mail(
            template,
            context,
            recipient,
            sender=self.queue.from_address,
            **kwargs,
        )
        recipients.add(recipient)
```

**仅新增一行**：`context["role"] = role`。因为 `context` 是字典引用，修改会影响本次发送的模板渲染，但 `"role"` 不是任何现有模板使用的变量名（模板变量来自 `safe_template_context()` 返回的 `ticket` / `queue` / `comment` 等），不会产生副作用。

---

### 7.3 埋点改造 3：`Ticket.send()` 中 ticket_cc 的发送尝试与跳过

**对应 PromQL 表达式**：
- `helpdesk_ticket_cc_skip_total{reason="queue_disabled"}` → §6.3.2 `HelpdeskTicketCCQueueDisabled`
- `helpdesk_ticket_cc_skip_total{reason="role_absent"}` → 调用方 roles 字典未含 `ticket_cc`
- `helpdesk_ticket_cc_skip_total{reason="recipient_empty"}` → SSO 用户 email 为空
- `helpdesk_ticket_cc_attempts_total` → §6.3.2 `HelpdeskTicketCCDeliveryDrop`

**指标类型**：`Counter`

**埋点文件**：`src/helpdesk/models.py`

**改造位置**：`Ticket.send()` 方法（第 638-694 行）

**原始代码**（`src/helpdesk/models.py:686-693`）：
```python
send("submitter", self.submitter_email)
send("ticket_cc", self.queue.updated_ticket_cc)
send("new_ticket_cc", self.queue.new_ticket_cc)
if self.assigned_to:
    send("assigned_to", self.assigned_to.email)
if self.queue.enable_notifications_on_email_events:
    for cc in self.ticketcc_set.all():
        send("ticket_cc", cc.email_address)
```

**改造后**（在 `Ticket.send()` 方法的 `send` 闭包定义之后、第一个 `send()` 调用之前，新增 Counter 定义和埋点逻辑）：

```python
# src/helpdesk/models.py — 在 Ticket.send() 方法内，send 闭包之后新增

from prometheus_client import Counter

HELPDESK_TICKET_CC_ATTEMPTS = Counter(
    "helpdesk_ticket_cc_attempts_total",
    "TicketCC notification attempts in Ticket.send()",
    ["queue_slug", "outcome"],
)

HELPDESK_TICKET_CC_SKIP = Counter(
    "helpdesk_ticket_cc_skip_total",
    "TicketCC notification skips in Ticket.send()",
    ["queue_slug", "reason"],
)

# 在 Ticket.send() 内部替换原来的第 686-693 行：

        send("submitter", self.submitter_email)

        # --- Queue 级 updated_ticket_cc ---
        queue_cc_addr = self.queue.updated_ticket_cc
        if queue_cc_addr and queue_cc_addr not in recipients and "ticket_cc" in roles:
            HELPDESK_TICKET_CC_ATTEMPTS.labels(
                queue_slug=self.queue.slug, outcome="attempted",
            ).inc()
            send("ticket_cc", queue_cc_addr)
        elif queue_cc_addr and "ticket_cc" not in roles:
            HELPDESK_TICKET_CC_SKIP.labels(
                queue_slug=self.queue.slug, reason="role_absent",
            ).inc()
        elif not queue_cc_addr:
            HELPDESK_TICKET_CC_SKIP.labels(
                queue_slug=self.queue.slug, reason="recipient_empty",
            ).inc()

        send("new_ticket_cc", self.queue.new_ticket_cc)

        if self.assigned_to:
            send("assigned_to", self.assigned_to.email)

        # --- 工单级 ticketcc_set ---
        if self.queue.enable_notifications_on_email_events:
            for cc in self.ticketcc_set.all():
                cc_addr = cc.email_address
                if cc_addr:
                    HELPDESK_TICKET_CC_ATTEMPTS.labels(
                        queue_slug=self.queue.slug, outcome="attempted",
                    ).inc()
                    send("ticket_cc", cc_addr)
                else:
                    # SSO 用户 email 为空
                    HELPDESK_TICKET_CC_SKIP.labels(
                        queue_slug=self.queue.slug, reason="recipient_empty",
                    ).inc()
        else:
            # Queue 开关关闭，所有 ticketcc_set 订阅者被跳过
            cc_count = self.ticketcc_set.count()
            if cc_count > 0:
                HELPDESK_TICKET_CC_SKIP.labels(
                    queue_slug=self.queue.slug, reason="queue_disabled",
                ).inc(cc_count)
```

**改造要点**：
1. `HELPDESK_TICKET_CC_ATTEMPTS` 记录每次实际尝试发送（outcome="attempted"）
2. `HELPDESK_TICKET_CC_SKIP` 记录跳过的原因（`queue_disabled` / `role_absent` / `recipient_empty`）
3. `queue_disabled` 分支用 `.inc(cc_count)` 一次递增，避免逐条遍历（Queue 关闭时不需要查 ticketcc_set 的具体内容）
4. `recipient_empty` 对应 SSO 用户 email 为空场景（`src/helpdesk/models.py:1946-1952` 的 `_email_address` 返回 `None`），被 `should_receive(None)` 过滤（`src/helpdesk/models.py:671-672`）

**对应的 PromQL 更新**（替换 §6.3.2 中的旧表达式）：
```yaml
# §6.3.2 HelpdeskTicketCCQueueDisabled 改用：
- alert: HelpdeskTicketCCQueueDisabled
  expr: increase(helpdesk_ticket_cc_skip_total{reason="queue_disabled"}[5m]) > 0

# §6.3.2 HelpdeskTicketCCDeliveryDrop 改用：
- alert: HelpdeskTicketCCDeliveryDrop
  expr: |
    rate(helpdesk_ticket_cc_attempts_total{outcome="attempted"}[1h])
    /
    (rate(helpdesk_ticket_cc_attempts_total{outcome="attempted"}[1h]) + rate(helpdesk_ticket_cc_skip_total[1h]))
    < 0.5

# §6.3.2 HelpdeskTicketCCSSOUserEmailEmpty 改用：
- alert: HelpdeskTicketCCSSOUserEmailEmpty
  expr: increase(helpdesk_ticket_cc_skip_total{reason="recipient_empty"}[5m]) > 0
```

---

### 7.4 埋点改造 4：`process_email_notifications_for_ticket_update` 中 assigned_to 通知跳过

**对应 PromQL 表达式**：
- `helpdesk_assigned_to_notification_skip_total{reason="settings_disabled"}`
- `helpdesk_assigned_to_notification_skip_total{reason="no_assigned_to"}`

**指标类型**：`Counter`

**埋点文件**：`src/helpdesk/update_ticket.py`

**改造位置**：`process_email_notifications_for_ticket_update()` 函数（第 128-188 行）

**原始代码**（`src/helpdesk/update_ticket.py:164-178`）：
```python
if ticket.assigned_to and (
    ticket.assigned_to.usersettings_helpdesk.email_on_ticket_change
    or (
        reassigned
        and ticket.assigned_to.usersettings_helpdesk.email_on_ticket_assign
    )
):
    messages_sent_to.update(
        ticket.send(
            {"assigned_to": (template_prefix + "owner", context)},
            dont_send_to=messages_sent_to,
            fail_silently=True,
            files=files,
        )
    )
```

**改造后**：
```python
# src/helpdesk/update_ticket.py — 文件顶部新增
from prometheus_client import Counter

HELPDESK_ASSIGNED_TO_NOTIFICATION_SKIP = Counter(
    "helpdesk_assigned_to_notification_skip_total",
    "Times assigned_to notification was skipped in process_email_notifications_for_ticket_update",
    ["reason"],
)

# 替换 update_ticket.py:164-178
if ticket.assigned_to:
    change_enabled = ticket.assigned_to.usersettings_helpdesk.email_on_ticket_change
    assign_enabled = ticket.assigned_to.usersettings_helpdesk.email_on_ticket_assign
    should_send = change_enabled or (reassigned and assign_enabled)
    if should_send:
        messages_sent_to.update(
            ticket.send(
                {"assigned_to": (template_prefix + "owner", context)},
                dont_send_to=messages_sent_to,
                fail_silently=True,
                files=files,
            )
        )
    else:
        # ← 埋点：assigned_to 存在但通知被 UserSettings 开关跳过
        HELPDESK_ASSIGNED_TO_NOTIFICATION_SKIP.labels(
            reason="settings_disabled",
        ).inc()
else:
    # ← 埋点：无 assigned_to
    HELPDESK_ASSIGNED_TO_NOTIFICATION_SKIP.labels(
        reason="no_assigned_to",
    ).inc()
```

**改造要点**：
1. 将原始的 `if ... and (...)` 条件拆分为三个变量 `change_enabled` / `assign_enabled` / `should_send`，便于在 else 分支精确打点
2. `reason="settings_disabled"` 对应 `email_on_ticket_change=False` 且 (`email_on_ticket_assign=False` 或非重新分配)，即**用户侧静默关闭通知**的场景
3. `reason="no_assigned_to"` 对应工单无负责人（非异常，但可用于统计）

**新增 PromQL**：
```yaml
- alert: HelpdeskAssignedToNotificationSkipped
  expr: increase(helpdesk_assigned_to_notification_skip_total{reason="settings_disabled"}[1h]) > 5
  for: 5m
  labels:
    severity: info
    team: sre
    category: helpdesk_notification
  annotations:
    summary: "assigned_to notifications skipped due to UserSettings in last 1h"
    description: "Users have email_on_ticket_change=False and email_on_ticket_assign=False. Source: src/helpdesk/update_ticket.py:164-170"
```

---

### 7.5 埋点改造 5：`create_usersettings` 管理命令中 UserSettings 补全计数

**对应 PromQL 表达式**：
- `helpdesk_usersettings_created_total`（新创建的 UserSettings 数量）
- `helpdesk_usersettings_missing_total`（仍缺失的 UserSettings 数量，需配合 `export_helpdesk_metrics` 导出）

**指标类型**：`Counter` + `Gauge`

**埋点文件**：`src/helpdesk/management/commands/create_usersettings.py`

**原始代码**（`src/helpdesk/management/commands/create_usersettings.py:30-33`）：
```python
def handle(self, *args, **options):
    """handle command line"""
    for u in User.objects.all():
        UserSettings.objects.get_or_create(user=u)
```

**改造后**：
```python
# src/helpdesk/management/commands/create_usersettings.py — 完整替换
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils.translation import gettext as _
from helpdesk.models import UserSettings
from prometheus_client import Counter, Gauge, CollectorRegistry, write_to_textfile
import time

User = get_user_model()

HELPDESK_USERSETTINGS_CREATED = Counter(
    "helpdesk_usersettings_created_total",
    "Number of UserSettings created by create_usersettings command",
)

HELPDESK_USERSETTINGS_MISSING = Gauge(
    "helpdesk_usersettings_missing_total",
    "Number of User rows without corresponding UserSettings",
)

HELPDESK_USERSETTINGS_MISSING_ASSIGNED = Gauge(
    "helpdesk_usersettings_missing_assigned_total",
    "Number of Users with assigned open tickets but no UserSettings",
)

HELPDESK_EXPORT_TIMESTAMP = Gauge(
    "helpdesk_usersettings_missing_timestamp_seconds",
    "Timestamp of last create_usersettings run",
)


class Command(BaseCommand):
    """create_usersettings command"""

    help = _(
        "Check for user without django-helpdesk UserSettings "
        "and create settings if required. Uses "
        "settings.DEFAULT_USER_SETTINGS which can be overridden to "
        "suit your situation."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--export-metrics",
            type=str,
            default=None,
            help="Path to write Prometheus .prom file (for node_exporter textfile collector)",
        )

    def handle(self, *args, **options):
        """handle command line"""
        created_count = 0
        for u in User.objects.all():
            _, created = UserSettings.objects.get_or_create(user=u)
            if created:
                created_count += 1
                HELPDESK_USERSETTINGS_CREATED.inc()

        if created_count:
            self.stdout.write(
                self.style.SUCCESS("Created %d UserSettings" % created_count)
            )
        else:
            self.stdout.write("All users have UserSettings already")

        # 导出 Gauge metrics（可选，通过 --export-metrics 参数）
        export_path = options.get("export_metrics")
        if export_path:
            missing = User.objects.exclude(
                pk__in=UserSettings.objects.values_list("user_id", flat=True)
            ).count()
            HELPDESK_USERSETTINGS_MISSING.set(missing)

            from helpdesk.settings import TICKET_OPEN_STATUSES
            missing_assigned = User.objects.filter(
                assigned_to__status__in=TICKET_OPEN_STATUSES
            ).exclude(
                pk__in=UserSettings.objects.values_list("user_id", flat=True)
            ).distinct().count()
            HELPDESK_USERSETTINGS_MISSING_ASSIGNED.set(missing_assigned)

            HELPDESK_EXPORT_TIMESTAMP.set(time.time())

            registry = CollectorRegistry()
            registry.register(HELPDESK_USERSETTINGS_MISSING)
            registry.register(HELPDESK_USERSETTINGS_MISSING_ASSIGNED)
            registry.register(HELPDESK_EXPORT_TIMESTAMP)
            write_to_textfile(export_path, registry)
            self.stdout.write("Metrics exported to %s" % export_path)
```

**改造要点**：
1. `get_or_create` 返回 `(obj, created)` 元组，原代码忽略了 `created` 布尔值，现在用 `Counter` 记录新创建的数量
2. `--export-metrics` 参数可选，不传时行为与原版一致（向后兼容）
3. Gauge 指标 `helpdesk_usersettings_missing_total` / `helpdesk_usersettings_missing_assigned_total` 与 §6.1.2 的告警表达式直接对应
4. `helpdesk_usersettings_missing_timestamp_seconds` 用于 §6.1.2 的 `HelpdeskCreateUserSettingsCronStale` 告警

**Cron 更新**：
```cron
0 3 * * * cd /path/to/project && /path/to/venv/bin/python manage.py create_usersettings --export-metrics /var/lib/node_exporter/helpdesk.prom >> /var/log/helpdesk-create-usersettings.log 2>&1
```

---

### 7.6 埋点改造 6：`TicketCC` 中 SSO 用户 email 为空检测

**对应 PromQL 表达式**：`helpdesk_ticketcc_sso_email_empty_total`

**指标类型**：`Gauge`（快照值，由 `export_helpdesk_metrics` 定期导出）

**埋点文件**：`src/helpdesk/management/commands/export_helpdesk_metrics.py`（§6.1.1 中新增的命令）

**改造位置**：在 `export_helpdesk_metrics.py` 的 `handle()` 方法中新增一段查询

**新增代码**（在 `handle()` 方法内、`write_to_textfile` 之前插入）：
```python
# 检测 TicketCC 中关联了 user 但 user.email 为空的订阅
HELPDESK_TICKETCC_SSO_EMAIL_EMPTY = Gauge(
    "helpdesk_ticketcc_sso_email_empty_total",
    "Number of TicketCC records where user is set but user.email is empty",
    registry=REGISTRY,
)

sso_email_empty_count = TicketCC.objects.filter(
    user__isnull=False,
    user__email="",
).count()
HELPDESK_TICKETCC_SSO_EMAIL_EMPTY.set(sso_email_empty_count)
```

**代码引用**：此 Gauge 对应 `src/helpdesk/models.py:1946-1952` 中 `_email_address` 属性的逻辑 — 当 `self.user` 存在但 `self.user.email` 为空字符串时，属性返回 `self.email`（可能也为空），导致 `should_receive` 返回 False（`src/helpdesk/models.py:671-672`），邮件被静默丢弃。

---

### 7.7 完整改造对应关系表

| §6 PromQL 表达式 | 指标类型 | 埋点改造节 | 埋点文件 | 埋点位置（行号） |
|------------------|---------|----------|---------|----------------|
| `helpdesk_email_sent_total{role, template_name, status}` | Counter | §7.1 | `src/helpdesk/templated_email.py` | 第 76 行（模板缺失 return 前）、第 128 行（msg.send() 成功后）、第 129-134 行（SMTPException except 内） |
| `helpdesk_smtp_failure_total{template_name}` | Counter | §7.1 | `src/helpdesk/templated_email.py` | 第 129-134 行（SMTPException except 内） |
| `helpdesk_template_missing_total{template_name}` | Counter | §7.1 | `src/helpdesk/templated_email.py` | 第 74-76 行（EmailTemplate.DoesNotExist 内层 except） |
| `context["role"] = role` 传递 | — | §7.2 | `src/helpdesk/models.py` | 第 676 行（send 闭包内，send_templated_mail 调用前） |
| `helpdesk_ticket_cc_attempts_total{queue_slug, outcome}` | Counter | §7.3 | `src/helpdesk/models.py` | 第 687 行（queue.updated_ticket_cc 发送前）、第 691-693 行（ticketcc_set 遍历内） |
| `helpdesk_ticket_cc_skip_total{queue_slug, reason}` | Counter | §7.3 | `src/helpdesk/models.py` | 第 687 行附近（queue_disabled / role_absent / recipient_empty 三种跳过） |
| `helpdesk_assigned_to_notification_skip_total{reason}` | Counter | §7.4 | `src/helpdesk/update_ticket.py` | 第 164-178 行（if 条件拆分后的 else 分支） |
| `helpdesk_usersettings_created_total` | Counter | §7.5 | `src/helpdesk/management/commands/create_usersettings.py` | 第 32-33 行（get_or_create 返回值解包） |
| `helpdesk_usersettings_missing_total` | Gauge | §7.5 | `src/helpdesk/management/commands/create_usersettings.py` | handle() 方法内 `--export-metrics` 分支 |
| `helpdesk_usersettings_missing_assigned_total` | Gauge | §7.5 | `src/helpdesk/management/commands/create_usersettings.py` | handle() 方法内 `--export-metrics` 分支 |
| `helpdesk_usersettings_missing_timestamp_seconds` | Gauge | §7.5 | `src/helpdesk/management/commands/create_usersettings.py` | handle() 方法内 `--export-metrics` 分支 |
| `helpdesk_ticketcc_sso_email_empty_total` | Gauge | §7.6 | `src/helpdesk/management/commands/export_helpdesk_metrics.py` | handle() 方法内（TicketCC user__email="" 查询） |

---

### 7.8 冒烟验证步骤（改造完成后按顺序执行）

#### 步骤 1：验证 metrics 端点可达

```bash
curl -s http://localhost:8000/metrics | grep helpdesk_
```

**预期输出**（至少包含以下行，值为 0 是正常的，因为还没有触发通知）：
```
# HELP helpdesk_email_sent_total Total helpdesk notification emails attempted
# TYPE helpdesk_email_sent_total counter
helpdesk_email_sent_total{role="unknown",template_name="",status="success"} 0
# HELP helpdesk_ticket_cc_attempts_total TicketCC notification attempts in Ticket.send()
# TYPE helpdesk_ticket_cc_attempts_total counter
helpdesk_ticket_cc_attempts_total{queue_slug="",outcome="attempted"} 0
# HELP helpdesk_usersettings_created_total Number of UserSettings created by create_usersettings command
# TYPE helpdesk_usersettings_created_total counter
helpdesk_usersettings_created_total 0
```

如果没有 `helpdesk_` 前缀的任何行，检查：
- `django_prometheus` 是否在 `INSTALLED_APPS`
- `PrometheusBeforeMiddleware` / `PrometheusAfterMiddleware` 是否在 `MIDDLEWARE`
- `/metrics/` URL 是否正确映射

#### 步骤 2：验证 UserSettings 补全 + Gauge 导出

```bash
# 执行命令并导出 metrics
python manage.py create_usersettings --export-metrics /tmp/helpdesk-test.prom

# 检查 .prom 文件内容
cat /tmp/helpdesk-test.prom | grep helpdesk_usersettings
```

**预期输出**：
```
# HELP helpdesk_usersettings_missing_total Number of User rows without corresponding UserSettings
# TYPE helpdesk_usersettings_missing_total gauge
helpdesk_usersettings_missing_total 0.0
# HELP helpdesk_usersettings_missing_assigned_total Number of Users with assigned open tickets but no UserSettings
# TYPE helpdesk_usersettings_missing_assigned_total gauge
helpdesk_usersettings_missing_assigned_total 0.0
# HELP helpdesk_usersettings_missing_timestamp_seconds Timestamp of last create_usersettings run
# TYPE helpdesk_usersettings_missing_timestamp_seconds gauge
helpdesk_usersettings_missing_timestamp_seconds 1.7e+09
```

如果 `helpdesk_usersettings_missing_total > 0`，说明有用户缺失 UserSettings，命令应该已经补全（下次再跑一次确认归零）。

#### 步骤 3：验证邮件发送 Counter

```bash
# 触发一次工单更新（创建评论）
python manage.py shell -c "
from helpdesk.models import Ticket
from helpdesk.update_ticket import update_ticket
from django.contrib.auth import get_user_model
User = get_user_model()
ticket = Ticket.objects.filter(assigned_to__isnull=False).first()
if ticket:
    update_ticket(
        user=User.objects.filter(is_staff=True).first(),
        ticket=ticket,
        comment='smoke test comment',
        public=True,
    )
    print(f'Updated ticket {ticket.ticket}')
else:
    print('No ticket with assigned_to found')
"

# 检查 Counter 是否增长
curl -s http://localhost:8000/metrics | grep 'helpdesk_email_sent_total{' | grep -v '0$'
```

**预期输出**（至少一行非零值）：
```
helpdesk_email_sent_total{role="assigned_to",template_name="updated_owner",status="success"} 1
helpdesk_email_sent_total{role="submitter",template_name="updated_submitter",status="success"} 1
helpdesk_email_sent_total{role="ticket_cc",template_name="updated_cc",status="success"} 1
```

如果只有 `role="unknown"` 的行，说明 §7.2 的 `context["role"] = role` 没有生效，检查 `Ticket.send()` 的改造是否正确。

#### 步骤 4：验证 ticket_cc 跳过 Counter

```bash
# 找一个 enable_notifications_on_email_events=False 的 Queue
python manage.py shell -c "
from helpdesk.models import Queue, Ticket
q = Queue.objects.filter(enable_notifications_on_email_events=False).first()
if q:
    ticket = Ticket.objects.filter(queue=q).first()
    if ticket:
        print(f'Queue {q.slug} has ticket {ticket.ticket}, CC disabled')
    else:
        print(f'Queue {q.slug} has no tickets')
else:
    print('No Queue with enable_notifications_on_email_events=False found')
"

# 触发一次该 Queue 工单的更新
# ... (同步骤 3 的 update_ticket 调用，但用该 Queue 的 ticket)

# 检查 skip Counter
curl -s http://localhost:8000/metrics | grep 'helpdesk_ticket_cc_skip_total'
```

**预期输出**：
```
helpdesk_ticket_cc_skip_total{queue_slug="your-queue-slug",reason="queue_disabled"} 1
```

#### 步骤 5：验证 assigned_to 跳过 Counter

```bash
# 将某个负责人的 email_on_ticket_change 设为 False
python manage.py shell -c "
from helpdesk.models import UserSettings, Ticket
from helpdesk.update_ticket import update_ticket
from django.contrib.auth import get_user_model
User = get_user_model()
ticket = Ticket.objects.filter(assigned_to__isnull=False).first()
if ticket:
    UserSettings.objects.filter(user=ticket.assigned_to).update(email_on_ticket_change=False)
    update_ticket(
        user=User.objects.filter(is_staff=True).first(),
        ticket=ticket,
        comment='test skip notification',
        public=True,
    )
    print('Done - check metrics')
else:
    print('No ticket with assigned_to found')
"

# 检查 skip Counter
curl -s http://localhost:8000/metrics | grep 'helpdesk_assigned_to_notification_skip_total'
```

**预期输出**：
```
helpdesk_assigned_to_notification_skip_total{reason="settings_disabled"} 1
```

#### 步骤 6：验证 Prometheus 能抓到所有 series

```bash
# 在 Prometheus 已配置 scrape 目标后，查询是否有数据
curl -s 'http://prometheus:9090/api/v1/query?query=helpdesk_email_sent_total' | python -m json.tool

# 或在 Prometheus UI 中查询：
# count(helpdesk_email_sent_total)
# 预期返回 > 0 的 value
```

#### 步骤 7：验证 Grafana 看板

导入 §6.4 的 JSON 骨架后：
1. 将所有 `datasource.uid` 替换为你的 Prometheus 数据源 UID
2. 将看板时间范围设为 "Last 5 minutes"
3. 执行一次工单更新操作
4. 刷新看板，确认以下面板有数据：
   - **Email Delivery Metrics**：应出现 role 分类的 success/failure 折线
   - **TicketCC subscriptions on disabled Queues**：如果存在 disabled Queue 应显示非零值
   - **Seconds since last create_usersettings run**：应 < 300（5 分钟内）

---

### 7.9 依赖的 settings 项汇总

| Settings 项 | 默认值 | 用途 | 影响的埋点 |
|------------|--------|------|----------|
| `HELPDESK_METRICS_ENABLED` | `True` | 全局开关，关闭后所有 `_inc_counter` 调用不执行 | §7.1, §7.3, §7.4 |
| `HELPDESK_DEFAULT_SETTINGS["email_on_ticket_change"]` | `True` | 新 UserSettings 的缺省值 | §7.4（决定 assigned_to 通知是否被跳过） |
| `HELPDESK_DEFAULT_SETTINGS["email_on_ticket_assign"]` | `True` | 新 UserSettings 的缺省值 | §7.4（决定分配通知是否被跳过） |
| Queue.`enable_notifications_on_email_events` | —（按 Queue 配置） | 控制 ticketcc_set 是否被遍历 | §7.3（决定 ticket_cc 跳过的 reason） |
| `HELPDESK_NOTIFY_SUBMITTER_FOR_ALL_TICKET_CHANGES` | `False` | 提交人是否接收所有变更 | 不直接影响埋点，但影响 submitter 角色的 Counter 值分布 |
| `HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS` | `False` | 私有 follow-up 是否跳过所有邮件 | 不直接影响埋点，但设为 True 时私有评论场景所有 Counter 不会增长 |

