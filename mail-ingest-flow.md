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

## 6. 流程时序图

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

## 7. 配置项速查

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
