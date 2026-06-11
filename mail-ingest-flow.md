# django-helpdesk 邮件收件管线（Mail Ingest Flow）分析

## 1. 整体架构概览

邮件收件管线的入口是 Django management command `get_email`，由 cron 或 shell 脚本定期调用：

```
cron / shell → manage.py get_email → process_email() → process_queue() → {pop3_sync | imap_sync | imap_oauth_sync | local_dir}
                                                                                           ↓
                                                                              extract_email_metadata()
                                                                                           ↓
                                                                       create_object_from_email_message()
```

**核心文件**：
- `src/helpdesk/management/commands/get_email.py` — 命令行入口
- `src/helpdesk/email.py` — 全部管线逻辑（1180 行）
- `src/helpdesk/exceptions.py` — 异常定义
- `src/helpdesk/lib.py` — `process_attachments()` 附件持久化
- `src/helpdesk/models.py` — Queue / Ticket / FollowUp / IgnoreEmail / FollowUpAttachment 模型

---

## 2. 五大阶段详解

### 2.1 邮件拉取（Fetch）

**代码位置**：`email.py:62-131`（`process_email`）、`email.py:378-499`（`process_queue`）

#### 调度机制

`process_email()` 遍历所有 `email_box_type IS NOT NULL AND allow_email_submission=True` 的 Queue，对每个 Queue 做频率控制：

```python
# email.py:102-107
queue_time_delta = timedelta(minutes=q.email_box_interval or 0)
if (q.email_box_last_check + queue_time_delta) < timezone.now():
    process_queue(q, logger=logger)
    q.email_box_last_check = timezone.now()
    q.save()
```

- `Queue.email_box_interval`：拉取间隔（分钟），默认 5
- `Queue.email_box_last_check`：上次成功拉取时间，拉取成功后更新

#### 四种拉取协议

| 协议 | 函数 | 连接方式 | 默认端口(SSL/非SSL) |
|------|------|----------|---------------------|
| POP3 | `pop3_sync()` | `poplib.POP3_SSL` / `poplib.POP3` | 995 / 110 |
| IMAP | `imap_sync()` | `imaplib.IMAP4_SSL` / `imaplib.IMAP4` | 993 / 143 |
| OAUTH | `imap_oauth_sync()` | `imaplib.IMAP4_SSL` + XOAUTH2 | 993 / 143 |
| Local | `process_queue()` 内联 | 读本地目录 | N/A |

#### 拉取行为

**POP3** (`email.py:134-192`)：
1. 尝试 STARTTLS → 失败则明文继续
2. `server.list()` 获取所有消息列表
3. 逐条 `server.retr(msgNum)` 拉取
4. 交给 `extract_email_metadata()` 处理
5. 成功 → `server.dele(msgNum)` 从服务器删除
6. `IgnoreTicketException` → 保留邮件
7. `DeleteIgnoredTicketException` → `server.dele()` 删除邮件
8. 返回 None → 保留邮件（**隐式重试**）

**IMAP** (`email.py:195-270`)：
1. 尝试 STARTTLS
2. 登录 → `server.select(folder)` 选择文件夹
3. `server.search(None, "NOT", "DELETED")` 搜索未删除消息
4. 逐条 `server.fetch(num, "(RFC822)")` 拉取
5. 成功 → `server.store(num, "+FLAGS", "\\Deleted")` 标记删除
6. `IgnoreTicketException` → 保留邮件
7. `DeleteIgnoredTicketException` → 标记删除
8. `TypeError` → **仅记录日志，邮件保留**（不删除，下次重试）
9. 最终 `server.expunge()` 清除已标记删除的邮件

**OAUTH** (`email.py:273-375`)：与 IMAP 类似，区别在认证方式用 `XOAUTH2`，且每次成功/删除后立即 `server.expunge()`。

**Local** (`email.py:456-498`)：
1. 扫描 `email_box_local_dir` 目录下的所有文件
2. 逐个读取文件内容
3. 成功 → `os.unlink(m)` 删除文件
4. `IgnoreTicketException` → 保留文件
5. `DeleteIgnoredTicketException` → `os.unlink(m)` 删除文件
6. `os.unlink` 失败 → 仅记录日志

---

### 2.2 邮件解析（Parse）

**代码位置**：`email.py:1044-1180`（`extract_email_metadata`）

解析流水线：

```
原始 RFC822 字符串
  ↓ email.message_from_string(message, EmailMessage, policy=policy.default)
EmailMessage 对象
  ↓ extract_email_subject()          → 去掉 Re:/FW:/Fw: 前缀
  ↓ 解析 sender_email               → email.utils.parseaddr(from_hdr)[1]
  ↓ IgnoreEmail 过滤                 → raise IgnoreTicketException / DeleteIgnoredTicketException
  ↓ get_ticket_id_from_subject_slug  → 正则匹配 [slug-id]
  ↓ extract_email_message_content()  → 提取正文 + HTML附件
  ↓ extract_attachments()            → 递归提取 MIME 附件
  ↓ add_file_if_always_save_incoming_email_message() → 可选保存原始 .eml
  ↓ 优先级解析                       → SMTP Priority/Importance 头
  ↓ 组装 payload dict
  ↓ create_object_from_email_message()
```

#### 正文提取逻辑 (`extract_email_message_content`, `email.py:858-943`)

1. `part.get_body()` 获取正文 MIME part
2. 若 `content_type == "multipart/related"` → 在 related part 内重新查找正文
3. 若 `content_type == "text/html"`：
   - 将 HTML 内容追加到 files 列表作为附件（`email_html_body.html`）
   - 尝试查找 `text/plain` part 替换正文
   - 若无 plain part → 用 BeautifulSoup 提取 HTML body 文本
4. 否则直接解码 MIME 内容
5. `parse_email_content()` 决定是否保留完整链式消息：
   - `HELPDESK_FULL_FIRST_MESSAGE_FROM_EMAIL=True` 且为新工单 → 保留完整内容
   - 否则 → `EmailReplyParser.parse_reply()` 只取实际回复部分

#### 附件提取逻辑 (`extract_attachments`, `email.py:975-1041`)

递归遍历 MIME 树：
- `multipart` 且非 `inline`/`attachment` → 递归子 part
- `multipart/related` → 使用 `iter_attachments()` 跳过正文 part
- 其他 → `process_as_attachment()` 加入 files 列表
- 用 `content_parts_excluded` 标志追踪正文 part 是否已被跳过

#### 忽略规则 (`extract_email_metadata:1087-1093`)

```python
for ignore in IgnoreEmail.objects.filter(Q(queues=queue) | Q(queues__isnull=True)):
    if ignore.test(sender_email):
        raise (
            IgnoreTicketException()
            if ignore.keep_in_mailbox
            else DeleteIgnoredTicketException()
        )
```

`IgnoreEmail.test()` (`models.py:1871-1897`) 支持四种匹配：
1. 完全匹配：`user@domain.com`
2. 用户名通配符：`*@domain.com`
3. 域名通配符：`user@*`
4. 全通配符：`*@*`

---

### 2.3 队列匹配（Queue Matching）

**代码位置**：`email.py:757-773`（`get_ticket_id_from_subject_slug`）、`email.py:1095-1126`

#### 主题匹配

```python
matchobj = re.match(r".*\[" + queue_slug + r"-(?P<id>\d+)\]", subject)
```

匹配格式：`[queue_slug-ticket_id]`，例如 `[support-42]`。

#### 跨队列查找流程 (`extract_email_metadata:1095-1126`)

1. 先用**当前 Queue 的 slug** 在主题中查找 ticket_id
2. 若未找到 → 遍历**所有其他 Queue**（`email_box_type__isnull=False`），逐一尝试 slug 匹配
3. 若在其他 Queue 中找到 → 切换到该 Queue 进行后续处理
4. 若所有 Queue 都未找到 → 恢复原始 Queue，创建新工单

#### In-Reply-To 匹配 (`create_object_from_email_message:607-614`)

```python
if in_reply_to is not None:
    queryset = FollowUp.objects.filter(message_id=in_reply_to).order_by("-date")
    if queryset.count() > 0:
        previous_followup = queryset.first()
        ticket = previous_followup.ticket
```

通过 `In-Reply-To` 邮件头在 FollowUp 表中查找已有工单。

**匹配优先级**：In-Reply-To → subject slug → ticket_id 参数 → 创建新工单

---

### 2.4 工单创建（Ticket Creation）

**代码位置**：`email.py:588-714`（`create_object_from_email_message`）

#### 创建/更新决策树

```
in_reply_to 匹配到 FollowUp?  → ticket = followup.ticket (更新)
                              ↓ No
ticket_id 参数存在?           → ticket = Ticket.objects.get(id=ticket_id)
                              ↓ No
QUEUE_EMAIL_BOX_UPDATE_ONLY?  → 返回 None (不创建)
                              ↓ False
创建新 Ticket                  → Ticket.objects.create(...)
```

#### 新工单创建

```python
ticket = Ticket.objects.create(
    title=payload["subject"],
    queue=queue,
    submitter_email=sender_email,
    created=now,
    description=payload["body"],
    priority=payload["priority"],
)
```

#### 已关闭工单的自动重开

```python
if ticket.status == Ticket.CLOSED_STATUS:
    ticket.status = Ticket.REOPENED_STATUS
    ticket.save()
```

#### FollowUp 创建

每封邮件都会创建一个 FollowUp：
- `title`: "E-Mail Received from {sender}" 或 "Ticket Re-Opened by E-Mail Received from {sender}"
- `comment`: `full_body`（如果有）否则 `body`
- `message_id`: 邮件的 Message-ID 头
- `public`: True
- `date`: `timezone.now()`

#### 信号发送

```python
if new:
    new_ticket_done.send(sender="create_object_from_email_message", ticket=ticket)
else:
    update_ticket_done.send(sender="create_object_from_email_message", followup=f)
```

这些信号可被 webhook 等外部模块接收（`webhooks.py`）。

---

### 2.5 附件处理（Attachment Processing）

**代码位置**：
- `email.py:946-962`（`process_as_attachment`）— MIME part → SimpleUploadedFile
- `lib.py:151-188`（`process_attachments`）— SimpleUploadedFile → FollowUpAttachment 模型
- `models.py:1273-1290`（`FollowUpAttachment`）— 附件持久化与路径

#### 两阶段处理

**阶段一：MIME → SimpleUploadedFile**（`email.py`，内存中）

- `extract_email_message_content()` 中：HTML 正文被包装为 `email_html_body.html` 附件
- `extract_attachments()` 中：每个非正文 MIME part 转为 `SimpleUploadedFile(name, payload_bytes, mime_type)`
- `add_file_if_always_save_incoming_email_message()` 中：可选保存完整原始 `.eml`

**阶段二：SimpleUploadedFile → FollowUpAttachment**（`lib.py`，数据库+文件系统）

```python
def process_attachments(followup, attached_files):
    for attached in attached_files:
        att = FollowUpAttachment(
            followup=followup,
            file=attached,
            filename=filename,
            mime_type=...,
            size=attached.size,
        )
        att.full_clean()    # ValidationError → 收集但不立即抛出
        att.save()
        if attached.size < max_email_attachment_size:  # 默认 512KB
            attachments.append([filename, att.file])   # 用于邮件通知
    if errors:
        raise ValidationError(list(errors))  # 所有附件验证完再统一抛出
```

#### 附件存储路径

```
helpdesk/attachments/{ticket_for_url}-{secret_key}/{followup_id}/{filename}
```

例如：`helpdesk/attachments/support-42-a1b2c3d4/15/report.pdf`

#### 附件大小限制

- `HELPDESK_MAX_EMAIL_ATTACHMENT_SIZE`：控制邮件通知中包含的附件大小（默认 512KB）
- 此限制**不影响附件保存到磁盘**，只影响是否将附件附加到通知邮件

---

## 3. 失败重试机制分析

### 3.1 隐式重试：邮件保留策略

系统**没有显式重试队列或重试计数器**。重试机制完全依赖"处理失败的邮件不删除，下次轮询时再次拉取"：

| 场景 | POP3 | IMAP | OAUTH | Local |
|------|------|------|-------|-------|
| `extract_email_metadata` 返回 None | 保留 | 保留 | 保留 | 保留 |
| `IgnoreTicketException` | 保留 | 保留 | 保留 | 保留 |
| `DeleteIgnoredTicketException` | 删除 | 删除 | 删除 | 删除 |
| `TypeError`（仅 IMAP/OAUTH） | — | 保留+日志 | 保留+日志 | — |
| `imaplib.IMAP4.error` | — | 整个搜索失败 | 整个搜索失败 | — |
| `process_queue` 整体异常 | 不更新 last_check | 不更新 last_check | 不更新 last_check | 不更新 last_check |

### 3.2 Queue 级别的失败处理

```python
# email.py:112-117
except Exception as e:
    logger.error(f"Queue processing failed: {q.slug} -- {e}", exc_info=True)
```

- 任何异常被捕获后，`email_box_last_check` **不会被更新**
- 这意味着下一次 `process_email()` 调用时，该 Queue 仍会满足 `last_check + interval < now` 条件
- **效果**：下次轮询会重新尝试该 Queue 的所有邮件

### 3.3 IMAP/OAUTH 登录失败 → 进程退出

```python
except imaplib.IMAP4.abort:
    logger.error("IMAP login failed...")
    server.logout()
    sys.exit()  # 进程退出，所有 Queue 停止处理
```

这是一个**严重行为**：单个 Queue 的 IMAP 登录失败会导致整个 `process_email()` 进程退出，后续 Queue 不会被处理。

### 3.4 缺失的重试机制

| 缺失项 | 影响 |
|--------|------|
| 无重试次数上限 | 同一封问题邮件会被无限重试拉取 |
| 无死信队列 | 无法将反复失败的邮件隔离 |
| 无指数退避 | 失败邮件每次轮询都尝试，浪费资源 |
| 无处理状态追踪 | 无法区分"未处理"和"处理失败" |
| IMAP 登录失败 → sys.exit | 阻断所有后续 Queue 处理 |
| 附件 ValidationError → 工单已创建 | 工单+FollowUp 已保存，但附件丢失，无回滚 |

---

## 4. 审计字段分析

### 4.1 Queue 审计字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `email_box_last_check` | DateTimeField | 上次成功拉取时间，`editable=False`，仅由 `get_email` 命令更新 |
| `email_box_interval` | IntegerField | 拉取间隔（分钟），默认 5 |

### 4.2 Ticket 审计字段

| 字段 | 类型 | 设置时机 | 说明 |
|------|------|----------|------|
| `created` | DateTimeField | `Ticket.save()` 当 `id` 为空时 | `timezone.now()`，仅首次创建 |
| `modified` | DateTimeField | `Ticket.save()` 每次保存时 | `timezone.now()`，包括 FollowUp 保存时联动更新 |
| `submitter_email` | EmailField | `Ticket.objects.create()` | 来自邮件 `From` 头 |
| `priority` | IntegerField | 创建时 | 从 SMTP Priority/Importance 头推断，否则默认 3 |
| `secret_key` | CharField(36) | 创建时 | `mk_secret()` 生成，用于附件路径和未登录访问 |
| `status` | IntegerField | 创建/更新时 | 创建时默认 OPEN，已关闭工单收到邮件自动改为 REOPENED |

**`modified` 的联动更新**：`FollowUp.save()` 会自动更新关联 Ticket 的 `modified`：

```python
# models.py:1047-1049
def save(self, *args, **kwargs):
    self.ticket.modified = timezone.now()
    self.ticket.save()
```

### 4.3 FollowUp 审计字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `date` | DateTimeField | 默认 `timezone.now()`，记录 FollowUp 创建时间 |
| `message_id` | CharField(256) | 邮件的 Message-ID 头，`editable=False`，用于 In-Reply-To 匹配 |
| `new_status` | IntegerField | 状态变更记录（如 REOPENED），可为空 |
| `public` | BooleanField | 邮件创建的 FollowUp 默认为 True |
| `user` | FK(User) | 邮件创建的 FollowUp 该字段为 None |

### 4.4 FollowUpAttachment 审计字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `filename` | CharField | 附件文件名 |
| `mime_type` | CharField | MIME 类型 |
| `size` | IntegerField | 文件大小（字节） |
| `file` | FileField | 文件内容，路径含 ticket secret_key |

### 4.5 IgnoreEmail 审计字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `date` | DateField | 添加日期，`editable=False`，首次保存时自动设置 |
| `keep_in_mailbox` | BooleanField | 决定被忽略邮件是保留还是删除 |
| `queues` | M2M(Queue) | 为空则匹配所有 Queue |

---

## 5. 关键混淆点澄清

### 5.1 `body` vs `full_body` vs `filtered_body` vs `mime_content`

| 变量 | 来源 | 用途 |
|------|------|------|
| `mime_content` | `mime_content_to_string()` | 从 MIME part 解码的原始文本 |
| `filtered_body` | `parse_email_content(mime_content, ...)` | 经过 EmailReplyParser 处理后的正文（去掉引用/转发） |
| `full_body` | 与 `mime_content` 相同 | 保留完整链式消息的原始正文 |
| `payload["body"]` | = `filtered_body` | 存入 Ticket.description |
| `payload["full_body"]` | = `full_body` | 存入 FollowUp.comment |

**区分规则**：
- 新工单且 `HELPDESK_FULL_FIRST_MESSAGE_FROM_EMAIL=True` → `filtered_body` = `full_body`（完整消息）
- 回复邮件 → `filtered_body` 仅包含实际回复，`full_body` 保留完整消息
- Ticket.description 用 `body`（filtered），FollowUp.comment 用 `full_body`

### 5.2 IgnoreTicketException vs DeleteIgnoredTicketException

| 异常 | 触发条件 | 邮件处理 |
|------|----------|----------|
| `IgnoreTicketException` | `ignore.keep_in_mailbox=True` | 保留在邮箱中 |
| `DeleteIgnoredTicketException` | `ignore.keep_in_mailbox=False` | 从邮箱中删除 |

### 5.3 两种工单匹配路径

| 路径 | 匹配方式 | 代码位置 |
|------|----------|----------|
| In-Reply-To 头 | `FollowUp.message_id` 匹配 | `email.py:607-614` |
| Subject slug | `[queue-id]` 正则匹配 | `email.py:1095-1126` |

两者独立运行，In-Reply-To 优先级更高。

### 5.4 附件处理的两个阶段

| 阶段 | 位置 | 数据形式 | 是否持久化 |
|------|------|----------|-----------|
| MIME 解析 | `email.py` | `SimpleUploadedFile`（内存） | 否 |
| 附件保存 | `lib.py:process_attachments()` | `FollowUpAttachment`（DB+文件系统） | 是 |

**关键风险**：如果 `process_attachments()` 抛出 `ValidationError`，此时 Ticket 和 FollowUp **已经保存成功**（在 `email.py:686` 之前），但附件丢失，无事务回滚。

### 5.5 `extract_email_metadata` 返回 None 的场景

唯一返回 None 的情况是 `QUEUE_EMAIL_BOX_UPDATE_ONLY=True` 且未匹配到已有工单时（`email.py:643-647`）。此时邮件被保留（不删除），下次轮询时会重新拉取，但结果仍为 None——**形成无限循环**。

---

## 6. 并发隐患深度分析

### 6.1 无任何并发控制机制

代码库中**完全没有**以下并发控制手段：

| 保护机制 | 是否存在 | 代码证据 |
|----------|----------|----------|
| 数据库唯一约束（message_id） | ❌ | `migrations/0022_add_submitter_email_id_field_to_ticket.py:12-23` 仅添加字段，无 `unique=True` |
| 数据库行锁（SELECT FOR UPDATE） | ❌ | `email.py` 中无 `select_for_update()` 调用 |
| 数据库事务包裹 | ❌ | `email.py` 中无 `transaction.atomic`，也未配置 `ATOMIC_REQUESTS` |
| 分布式锁（如 Redis lock） | ❌ | 无相关代码 |
| 处理状态标记（processing/processed） | ❌ | Ticket/FollowUp 模型无相关字段 |
| 进程级互斥锁（如 PID 文件） | ❌ | `get_email.py` 中无相关逻辑 |

### 6.2 邮件删除时机分析

所有协议的删除时机都**晚于**业务处理完成：

**POP3** (`email.py:148-190`):
```
server.list() → server.retr(msgNum) → extract_email_metadata() → server.dele(msgNum)
```

**IMAP** (`email.py:224-261`):
```
server.search() → server.fetch(num, "(RFC822)") → extract_email_metadata() → server.store(num, "+FLAGS", "\\Deleted")
```

**关键问题**：两个进程可以同时 `list()`/`search()` 到同一封邮件，同时 `retr()`/`fetch()` 拉取，各自处理成功后各自删除。

### 6.3 竞态场景一：新邮件（无 In-Reply-To）

当 cron 间隔 < 单次拉取耗时，两个进程重叠运行时：

```
时间线：
T0: 进程1 list() → 看到邮件 #5
T0: 进程2 list() → 看到邮件 #5
T1: 进程1 retr(5) → 获取完整邮件内容
T1: 进程2 retr(5) → 获取完整邮件内容
T2: 进程1 extract_email_metadata() → ticket_id=None, in_reply_to=None
T2: 进程2 extract_email_metadata() → ticket_id=None, in_reply_to=None
T3: 进程1 FollowUp.objects.filter(message_id=None) → None
T3: 进程2 FollowUp.objects.filter(message_id=None) → None
T4: 进程1 Ticket.objects.create() → 工单 #1001
T4: 进程2 Ticket.objects.create() → 工单 #1002
T5: 进程1 FollowUp.save() → 关联工单 #1001，message_id="<msg-123@example.com>"
T5: 进程2 FollowUp.save() → 关联工单 #1002，message_id="<msg-123@example.com>"
T6: 进程1 process_attachments() → 附件保存到工单 #1001
T6: 进程2 process_attachments() → 附件保存到工单 #1002
T7: 进程1 send_info_email() → 发通知邮件
T7: 进程2 send_info_email() → 发通知邮件
T8: 进程1 new_ticket_done.send() → 触发 webhook
T8: 进程2 new_ticket_done.send() → 触发 webhook
T9: 进程1 server.dele(5) → 标记删除
T9: 进程2 server.dele(5) → 标记删除
T10: 进程1 server.quit() → 真正删除
T10: 进程2 server.quit() → 真正删除

结果：两张内容完全相同的工单 #1001 和 #1002，各自有 FollowUp，各自有附件，各自发送了通知。
```

**核心代码路径**（`email.py:629-641`）：
```python
if ticket is None:
    if not getattr(settings, "QUEUE_EMAIL_BOX_UPDATE_ONLY", False):
        ticket = Ticket.objects.create(
            title=payload["subject"],
            queue=queue,
            submitter_email=sender_email,
            created=now,
            description=payload["body"],
            priority=payload["priority"],
        )
        ticket.save()
        new = True
```

由于没有事务，两个进程都会通过 `ticket is None` 的判断，各自创建 Ticket。

### 6.4 竞态场景二：回复邮件（有 In-Reply-To）

```
时间线：
T0: 进程1 FollowUp.objects.filter(message_id="<prev-456@example.com>") → 找到 FollowUp #50 → 工单 #800
T0: 进程2 FollowUp.objects.filter(message_id="<prev-456@example.com>") → 找到 FollowUp #50 → 工单 #800
T1: 进程1 FollowUp.save() → 创建 FollowUp #51，关联工单 #800
T1: 进程2 FollowUp.save() → 创建 FollowUp #52，关联工单 #800
T2: 进程1 process_attachments() → 附件保存到 FollowUp #51
T2: 进程2 process_attachments() → 附件保存到 FollowUp #52

结果：工单 #800 下有两条内容完全相同的 FollowUp #51 和 #52，各自有附件。
```

**核心代码路径**（`email.py:607-614`）：
```python
if in_reply_to is not None:
    queryset = FollowUp.objects.filter(message_id=in_reply_to).order_by("-date")
    if queryset.count() > 0:
        previous_followup = queryset.first()
        ticket = previous_followup.ticket
```

由于两个进程同时查询，都能找到同一个 Ticket，但都会创建各自的 FollowUp。

### 6.5 In-Reply-To 和主题匹配在竞态下的有效性

| 匹配方式 | 防止重复工单 | 防止重复 FollowUp | 说明 |
|----------|-------------|------------------|------|
| In-Reply-To | ✅ 对回复邮件 | ❌ | 能匹配到同一 Ticket，但两个进程各创建 FollowUp |
| 主题 slug 匹配 | ✅ 对回复邮件 | ❌ | 同上 |
| 两者都无（新邮件） | ❌ | ❌ | 两个进程各自创建 Ticket 和 FollowUp |

**结论**：这两种机制是**业务去重**手段，不是**并发去重**手段。它们在单进程串行时工作良好，但在并发重叠时无法防止重复。

### 6.6 并发防护的可行方案

基于代码现状，可采用以下修复方案：

**方案 A：数据库唯一约束（推荐）**
```sql
-- 在 FollowUp 表上添加唯一约束
ALTER TABLE helpdesk_followup ADD CONSTRAINT unique_message_id UNIQUE(message_id);
```
配合代码中捕获 `IntegrityError`，第二个进程会因唯一约束冲突而失败，邮件保留在服务器，下次重试时通过 In-Reply-To 匹配到已有工单（但此时已有 FollowUp，会走更新流程）。

**方案 B：分布式锁**
在 `process_email()` 入口添加基于 Redis 的锁，确保同一时间只有一个进程在运行。

**方案 C：先标记删除再处理**
修改拉取逻辑，先 `store(+FLAGS, "\\Deleted")` 标记删除，再处理。但需要处理回滚逻辑。

---

## 7. 残缺工单分析

### 7.1 执行顺序与事务边界

`create_object_from_email_message()` (`email.py:588-714`) 的执行顺序：

```
1. 查询已有 Ticket (In-Reply-To / ticket_id)
   ↓
2. 无匹配 → Ticket.objects.create()  ← Ticket 已持久化
   ↓
3. FollowUp.save()                    ← FollowUp 已持久化，同时触发 Ticket.modified 更新
   ↓
4. process_attachments(f, files)      ← 可能抛出 ValidationError
   ↓
5. create_ticket_cc()                 ← 创建抄送关系
   ↓
6. send_info_email()                  ← 发送通知邮件
   ↓
7. new_ticket_done.send() / update_ticket_done.send()  ← 发送信号
   ↓
8. return ticket                      ← 返回成功，邮件将被删除
```

**关键代码**（`email.py:683-694`）：
```python
if helpdesk_settings.HELPDESK_ENABLE_ATTACHMENTS:
    try:
        attached = process_attachments(f, files)
    except ValidationError as e:
        logger.error(str(e))  # 仅记录日志，不回滚
    else:
        for att_file in attached:
            logger.info("Attachment '%s' successfully added...", att_file[0])
```

由于**没有事务包裹**，步骤 2 和 3 的数据一旦写入就永久保存。步骤 4 抛出异常后，仅被捕获并记录日志，函数继续执行后续步骤。

### 7.2 process_attachments 的失败原因

`process_attachments()` (`lib.py:151-188`) 可能抛出 `ValidationError` 的场景：

```python
def process_attachments(followup, attached_files):
    errors = set()
    for attached in attached_files:
        att = FollowUpAttachment(
            followup=followup,
            file=attached,
            filename=filename,
            mime_type=...,
            size=attached.size,
        )
        try:
            att.full_clean()  # ← 验证失败抛出 ValidationError
        except ValidationError as e:
            errors.add(e)
        else:
            att.save()        # ← 保存失败也可能抛出异常
    
    if errors:
        raise ValidationError(list(errors))  # ← 所有附件验证完统一抛出
```

可能的失败原因：
1. `att.full_clean()` 验证失败：文件名非法、文件大小超限、MIME 类型不被允许等
2. `att.save()` 失败：磁盘满、权限不足、文件系统错误等
3. `upload_to` 路径生成失败

**注意**：循环内部分附件可能已 `save()` 成功，只有验证失败的会被收集。但一旦有任何验证失败，最后会统一抛出 `ValidationError`，**已成功保存的附件不会回滚**。

### 7.3 残缺工单的最终状态

| 字段 | 值 | 说明 |
|------|----|------|
| `Ticket.status` | OPEN | 正常打开状态，无任何异常标记 |
| `Ticket.created` | 正确时间 | 已正确设置 |
| `Ticket.modified` | 正确时间 | 由 `FollowUp.save()` 联动更新 |
| `Ticket.description` | 邮件正文 | 已正确保存 |
| `FollowUp.comment` | 邮件完整正文 | 已正确保存 |
| `FollowUp.message_id` | 邮件 Message-ID | 已正确设置 |
| `FollowUp.followupattachment_set.count()` | 0 或 部分 | 部分成功的附件可能已保存 |
| `Ticket.ticketcc_set` | 可能已创建 | CC 关系在异常捕获后创建 |
| 通知邮件 | 已发送 | `send_info_email()` 在异常捕获后执行 |
| 信号 | 已发送 | `new_ticket_done` 在异常捕获后发送 |
| 原始邮件 | 已删除 | 函数返回 ticket（非 None），触发删除 |

**没有任何数据库字段标记这是一个"残缺"工单**。它看起来就是一个正常的、没有附件的工单。

### 7.4 残缺工单的发现机制

**自动化发现**：
1. 查看日志中是否有 `logger.error(str(e))` 记录（`email.py:687`）
2. 日志内容包含 `ValidationError` 的详细信息

**手动发现**：
1. 人工对比邮件内容和工单内容，发现附件缺失
2. 定期查询 `FollowUp.objects.filter(message_id__isnull=False, followupattachment__isnull=True)` 找出有 message_id 但无附件的 FollowUp

### 7.5 残缺工单的补救方法

由于原始邮件已被从服务器删除，补救手段有限：

1. **从邮件服务器备份恢复**：如果邮件服务器有备份，可手动恢复邮件到收件箱，下次轮询时会重新处理。但需要注意：
   - 重新处理会创建新的 FollowUp（因为已有 Ticket）
   - 如果 message_id 上有唯一约束，会失败

2. **从发件人重新获取**：联系发件人重新发送邮件，然后手动关联到已有工单。

3. **文件系统层面恢复**：如果 `att.save()` 部分成功但整体抛出异常，已保存的附件文件可能仍在磁盘上。可通过 FollowUp ID 在 `helpdesk/attachments/` 目录中查找。

4. **代码层面的预防修复**：
   - 添加 `transaction.atomic` 包裹整个流程
   - 先验证所有附件，再统一保存（当前实现是边验证边保存）
   - 附件处理失败时返回 None，让邮件保留在服务器上重试

---

## 8. 流程时序图

```
┌─────────┐     ┌──────────┐     ┌──────────────┐     ┌──────────┐     ┌────────────┐
│  cron   │     │process_  │     │process_queue │     │extract_  │     │create_obj  │
│/manage  │────▶│email()   │────▶│/pop3_sync/   │────▶│email_    │────▶│from_email  │
│get_email│     │          │     │imap_sync/    │     │metadata()│     │_message()  │
└─────────┘     │ per Queue│     │imap_oauth/   │     │          │     │            │
                └──────────┘     │local_dir     │     │解析Subject│     │创建Ticket  │
                                 └──────────────┘     │解析Sender │     │创建FollowUp│
                                                      │忽略规则   │     │保存附件    │
                                                      │队列匹配   │     │发送通知    │
                                                      │提取正文   │     │发送信号    │
                                                      │提取附件   │     └────────────┘
                                                      │解析优先级 │
                                                      └────────────┘
```

---

## 9. 配置项速查

| 配置项 | 默认值 | 作用 | 代码位置 |
|--------|--------|------|----------|
| `QUEUE_EMAIL_BOX_TYPE` | None | 全局默认邮箱类型 | `email.py:405` |
| `QUEUE_EMAIL_BOX_USER` | None | 全局默认邮箱用户名 | `email.py:142,203` |
| `QUEUE_EMAIL_BOX_PASSWORD` | None | 全局默认邮箱密码 | `email.py:143,204` |
| `QUEUE_EMAIL_BOX_SSL` | False | 全局默认是否 SSL | `email.py:444` |
| `QUEUE_EMAIL_BOX_HOST` | None | 全局默认邮箱主机 | `email.py:450` |
| `HELPDESK_FULL_FIRST_MESSAGE_FROM_EMAIL` | False | 新工单是否保留完整链式消息 | `email.py:1130-1134` |
| `HELPDESK_ALWAYS_SAVE_INCOMING_EMAIL_MESSAGE` | False | 是否保存原始 .eml 文件作为附件 | `email.py:779` |
| `HELPDESK_ENABLE_ATTACHMENTS` | True | 是否启用附件处理 | `email.py:683,1143` |
| `HELPDESK_MAX_EMAIL_ATTACHMENT_SIZE` | 512000 | 通知邮件中附件大小上限(字节) | `lib.py:152-153` |
| `QUEUE_EMAIL_BOX_UPDATE_ONLY` | False | 仅更新已有工单，不创建新工单 | `email.py:630` |
| `HELPDESK_OAUTH` | — | OAuth 配置字典 | `email.py:286-287` |
| `HELPDESK_IMAP_DEBUG_LEVEL` | — | IMAP 调试级别 | `email.py:298` |
