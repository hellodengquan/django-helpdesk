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

