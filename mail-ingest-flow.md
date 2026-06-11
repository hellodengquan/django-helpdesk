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

### 3.4 OAuth 授权续期与失败兜底链路

#### 3.4.1 令牌获取方式：每次重新拉取，无续期概念

`imap_oauth_sync()` (`email.py:273-375`) 使用 **Client Credentials** 授权模式（`oauth2lib.BackendApplicationClient`），**没有令牌缓存，也没有 refresh token 续期机制**。每次调用都会重新获取令牌：

```python
# email.py:285-296
client = oauth2lib.BackendApplicationClient(
    client_id=helpdesk_settings.HELPDESK_OAUTH["client_id"],
    scope=helpdesk_settings.HELPDESK_OAUTH["scope"],
)

oauth = requests_oauthlib.OAuth2Session(client=client)
token = oauth.fetch_token(
    token_url=helpdesk_settings.HELPDESK_OAUTH["token_url"],
    client_id=helpdesk_settings.HELPDESK_OAUTH["client_id"],
    client_secret=helpdesk_settings.HELPDESK_OAUTH["secret"],
    include_client_id=True,
)
```

**关键事实**：
- 每次 `process_queue()` 调用 `imap_oauth_sync()` 都会走完整的 `fetch_token` 流程
- 令牌只在本次 `imap_oauth_sync()` 调用内有效
- 函数返回后令牌即丢失，不缓存也不持久化
- 不存在"令牌过期自动续期"的逻辑，因为每次都是全新获取

**配置项** (`settings.py:429-433`)：
```python
HELPDESK_OAUTH = {
    "token_url": "",   # 令牌端点 URL
    "client_id": "",   # 客户端 ID
    "secret": "",      # 客户端密钥
    "scope": [""],     # 权限范围
}
```

#### 3.4.2 令牌获取失败的异常传播路径

`imap_oauth_sync()` 的异常捕获只有两种：

| 异常类型 | 捕获位置 | 处理方式 |
|----------|----------|----------|
| `imaplib.IMAP4.abort` | `email.py:309-312` | 记录 error 日志 → `server.logout()` → **`sys.exit()`** |
| `ssl.SSLError` | `email.py:314-320` | 记录 error 日志 → `server.logout()` → **`sys.exit()`** |
| `fetch_token` 失败（网络/认证错误） | 不捕获 | 向上抛出到 `process_email()` |
| `imaplib.IMAP4.error`（搜索/拉取失败） | `email.py:367-371` | 记录 error 日志，继续执行到 `server.logout()` |
| `TypeError`（单封邮件处理失败） | `email.py:347-351` | 记录 error 日志，跳过该邮件继续处理下一封 |

**`fetch_token` 失败的具体传播链**：

```
oauth.fetch_token() 失败（如网络超时、客户端凭据无效、token_url 不可达）
  ↓ 未被 imap_oauth_sync() 内的 except 捕获
  ↓ 向上抛出到 process_queue()
  ↓ process_queue() 没有 try-except 包裹整体（只有局部）
  ↓ 继续向上抛出到 process_email() 的 except Exception as e
  ↓ 记录 error 日志 + exc_info 堆栈
  ↓ 该 Queue 的 email_box_last_check 不更新
  ↓ 继续遍历下一个 Queue
```

与 IMAP 登录失败不同，**OAuth 的 `fetch_token` 失败不会导致 `sys.exit()`**，因为它抛出的不是 `imaplib.IMAP4.abort`。

#### 3.4.3 失败后的邮件状态

| 失败阶段 | 邮件状态 | 后续行为 |
|----------|----------|----------|
| `fetch_token` 失败 | 全部保留在服务器 | 整次拉取失败，所有邮件都在服务器，下次轮询重试 |
| `server.authenticate()` 失败（IMAP4.abort） | 全部保留在服务器 | **`sys.exit()` 进程退出**，所有后续 Queue 都不处理 |
| `server.search()` 失败（IMAP4.error） | 全部保留在服务器 | 记录日志，`server.expunge()` + `logout()` 正常退出，下一轮重试 |
| 单封邮件 `TypeError` 处理失败 | 该邮件保留在服务器 | 跳过该邮件，继续处理下一封 |
| 单封邮件返回 None（UPDATE_ONLY） | 该邮件保留在服务器 | 跳过该邮件，继续处理下一封 |

#### 3.4.4 队列级联停摆风险

**IMAP/OAuth 登录失败导致的 `sys.exit()` 是最严重的故障模式**：

```
时间线：
T0: process_email() 开始遍历 Queue 列表 [Q1, Q2, Q3, ...]
T1: 处理 Q1（普通 POP3）→ 成功
T2: 处理 Q2（IMAP OAuth）→ fetch_token 成功 → server.authenticate() 失败 → IMAP4.abort
T3: Q2 的异常处理 → logger.error(...) → sys.exit()
T4: 进程终止
结果：Q3、Q4... 全部不会被处理
```

**代码证据**（`email.py:309-312`）：
```python
except imaplib.IMAP4.abort as e1:
    logger.error(f"IMAP authentication failed in OAUTH: {e1}", exc_info=True)
    server.logout()
    sys.exit()  # 整个进程退出
```

这意味着：**一个 Queue 的 OAuth 认证故障会导致所有 Queue 的邮件拉取停摆**。

### 3.5 缺失的重试机制

| 缺失项 | 影响 |
|--------|------|
| 无重试次数上限 | 同一封问题邮件会被无限重试拉取 |
| 无死信队列 | 无法将反复失败的邮件隔离 |
| 无指数退避 | 失败邮件每次轮询都尝试，浪费资源 |
| 无处理状态追踪 | 无法区分"未处理"和"处理失败" |
| IMAP/OAuth 登录失败 → sys.exit | 阻断所有后续 Queue 处理 |
| 附件 ValidationError → 工单已创建 | 工单+FollowUp 已保存，但附件丢失，无回滚 |
| OAuth 令牌无缓存 | 每次拉取都重新获取令牌，增加延迟和失败概率 |

### 3.6 坏邮件解析的健壮性边界

#### 3.6.1 单封邮件的异常捕获边界

四种拉取协议对单封邮件处理的异常捕获范围各不相同：

| 异常类型 | POP3 | IMAP | OAuth | Local |
|----------|------|------|-------|-------|
| `IgnoreTicketException` | ✅ 捕获，保留邮件 | ✅ 捕获，保留邮件 | ✅ 捕获，保留邮件 | ✅ 捕获，保留文件 |
| `DeleteIgnoredTicketException` | ✅ 捕获，删除邮件 | ✅ 捕获，删除邮件 | ✅ 捕获，删除邮件 | ✅ 捕获，删除文件 |
| `TypeError` | ❌ 不捕获 | ✅ 捕获，记 error 日志，保留邮件保留 | ✅ 捕获，记 error 日志，邮件保留 | ❌ 不捕获 |
| `MessageParseError` (MIME 损坏) | ❌ 不捕获 | ❌ 不捕获 | ❌ 不捕获 | ❌ 不捕获 |
| `UnicodeError` / 编码错误 | ❌ 不捕获（大部分编码问题被兜底） | ❌ 不捕获（大部分编码问题被兜底） | ❌ 不捕获 | ❌ 不捕获 |
| `ValidationError` (附件) | ❌ 不捕获（但在函数内被 try-except 包住） | ❌ 不捕获（但在函数内被 try-except 包住） | ❌ 不捕获 | ❌ 不捕获 |
| `MemoryError` (超大邮件) | ❌ 不捕获 | ❌ 不捕获 | ❌ 不捕获 | ❌ 不捕获 |
| 其他任意未捕获异常 | ❌ 冒泡到外层 | ❌ 部分被外层 IMAP4.error 可能捕获 | ❌ 部分被外层 IMAP4.error 可能捕获 | ❌ 冒泡到外层 |

**关键结论**：只有 `IgnoreTicketException` 和 `DeleteIgnoredTicketException` 在所有协议中都被逐封捕获。其他绝大多数异常都会冒泡。

**代码证据**：
- POP3 (`email.py:166-190`)：只有两个 except 分支
- IMAP (`email.py:232-261`)：三个 except 分支，多了一个 `TypeError`
- OAuth (`email.py:332-365`)：同 IMAP
- Local (`email.py:468-498`)：只有两个 except 分支

IMAP/OAuth 外层还有一个 `except imaplib.IMAP4.error` 包裹整个 search + 循环，但只捕获 IMAP 协议层面的错误，不捕获业务逻辑抛出的 Python 异常。

#### 3.6.2 场景一：MIME 结构损坏

**触发路径**：

```
email.message_from_string(message, EmailMessage, policy=policy.default)
  ↓
如果邮件格式严重损坏（缺少必要的 MIME 边界、畸形头部等
  ↓
抛出 email.errors.MessageParseError（或其子类 MessageError）
  ↓
extract_email_metadata() 内未捕获
  ↓
pop3_sync/imap_sync 单封邮件循环未捕获
  ↓
向上冒泡
```

**代码位置**：`email.py:1072-1073`

```python
message_obj: EmailMessage = email.message_from_string(
    message, EmailMessage, policy=policy.default
)
```

使用 `policy=policy.default` 是**严格模式**，对损坏邮件的容错性比 `policy.compat32` 低很多。

**异常传播后果**：

| 协议 | 传播结果 |
|------|----------|
| **POP3** | 冒泡 → `process_queue()` 无捕获 → `process_email()` 的 `except Exception` 捕获 → **该 Queue 本轮停止，`email_box_last_check` 不更新 → 继续下一个 Queue |
| **IMAP** | 冒泡 → 外层 `except imaplib.IMAP4.error` **不捕获**（MessageParseError 不属于 IMAP4.error → 继续冒泡到 `process_email()` → 同上 |
| **OAuth** | 同 IMAP |
| **Local** | 冒泡 → `process_queue()` 无捕获 → `process_email()` 捕获 → 同上 |

**对队列的影响**：
- ✅ 其他 Queue 不受影响（`process_email()` 最外层有 `except Exception`）
- ❌ 当前 Queue **后面的所有邮件都不会被处理
- ❌ 已处理过的邮件（在损坏邮件之前的）已正常删除
- ❌ 损坏的邮件保留在服务器，下次轮询还会再次尝试 → **无限重试
- ❌ 每次重试都失败 → 该 Queue 永久卡在这封坏邮件上

**部分损坏的 MIME（能解析出部分内容）：
- 正文提取失败有兜底：`extract_email_message_content()` 中如果 `part.get_body()` 返回 None → 返回 `(None, None)` → 后续用空字符串继续
- 附件提取失败有兜底：`extract_attachments()` 递归中如果某个 part 解析失败可能抛出异常 → 整体中断

#### 3.6.3 场景二：编码不匹配

**多层编码兜底机制非常完善，大部分编码问题都有 fallback**：

**第 1 层：拉取阶段**
```python
# email.py:165 (POP3), 231 (IMAP), 330 (OAuth), 467 (Local)
full_message = encoding.force_str(data[0][1], errors="replace")
```
`errors="replace"` → 无法解码的字节用 替换字符替代，**永远不会抛出 UnicodeError。

**第 2 层：主题解码**
```python
# email.py:501-509 (decodeUnknown)
def decodeUnknown(charset, string):
    if string and not isinstance(string, str):
        if not charset:
            try:
                return str(string, encoding="utf-8", errors="replace")
            except UnicodeError:
                return str(string, encoding="iso8859-1", errors="replace")
        return str(string, encoding=charset, errors="replace")
    return string
```
全部使用 `errors="replace"`，且无字符集时先试 UTF-8，失败回退到 ISO-8859-1。

**第 3 层：邮件头解码**
```python
# email.py:512-519
def decode_mail_headers(string):
    decoded = email.header.decode_header(string)
    return " ".join([
        str(msg, encoding=charset, errors="replace") if charset else str(msg)
        for msg, charset in decoded
    ])
```
同样 `errors="replace"`。

**第 4 层：正文解码**
```python
# email.py:805-811
def get_email_body_from_part_payload(part) -> str:
    try:
        return encoding.smart_str(part.get_payload(decode=True))
    except UnicodeDecodeError:
        return encoding.smart_str(part.get_payload(decode=False))
```
解码失败就不解码，直接返回原始字节串。

**但仍可能抛出编码异常的边缘情况**：

1. **无效字符集名称**：
   - `charset = "invalid-charset-xyz"`
   - `str(string, encoding=charset, errors="replace")` 会抛出 `LookupError: unknown encoding`
   - `decodeUnknown()` 未捕获 `LookupError`
   - 异常会冒泡

2. **Base64 解码失败**：
   - `part.get_payload(decode=True)` 在 base64 数据损坏时可能抛出 `binascii.Error`
   - `mime_content_to_string()` 未捕获
   - 异常会冒泡

3. **Quoted-Printable 解码失败**：
   - 同理可能抛出 `binascii.Error`
   - 未捕获

**编码异常的传播后果**：与 MIME 损坏相同 → 当前 Queue 停止，后续邮件全部跳过。

#### 3.6.4 场景三：大体积邮件 / 大附件

**代码中完全没有大小限制**：

- **整封邮件全部读入内存：`full_message` 是完整的邮件字符串
- **附件全部读入内存：`SimpleUploadedFile(name, payload_bytes, ...)`
- **原始邮件备份也在内存：`add_file_if_always_save_incoming_email_message()` 又复制一份

**可能触发的问题**：

1. **内存不足 (MemoryError)**
   - 超大邮件（比如几十上百 MB 的附件
   - 整个邮件内容全部加载到内存
   - 可能导致 OOM，进程被系统 kill
   - **无日志，无痕迹，`email_box_last_check` 不更新

2. **磁盘空间不足**
   - 附件保存到磁盘时可能失败
   - `att.save()` 抛出异常
   - 但在 `create_object_from_email_message()` 中被 `except ValidationError` 只捕获 `ValidationError`
   - 如果是 `OSError`/`IOError` 等磁盘错误，**不会被捕获**，会冒泡

3. **Django 文件字段 max_length 限制**
   - `Attachment.file` 是 `FileField(max_length=1000)`
   - 文件名路径超过 1000 字符可能抛出 `ValidationError`
   - 会被 `process_attachments()` 的 `full_clean()` 捕获 → 收集到 errors → 抛出 `ValidationError`
   - **但这个 ValidationError 会被 `create_object_from_email_message()` 中的 `except ValidationError as e: logger.error(str(e))` 捕获
   - 所以：工单和 FollowUp 已保存，附件丢失（即残缺工单）

**大邮件对队列的影响**：
- 如果抛出可捕获的异常（如 ValidationError）→ 单封邮件处理完，继续下一封
- 如果抛出未捕获的异常（如 OOM、OSError）→ 当前 Queue 停摆，后续邮件全部跳过
- 如果 OOM 导致进程直接退出 → 所有 Queue 都停摆

#### 3.6.5 异常传播链路总览

```
单封邮件处理开始
  │
  ├─→ IgnoreTicketException → 捕获 → 邮件保留 → 继续下一封
  │
  ├─→ DeleteIgnoredTicketException → 捕获 → 邮件删除 → 继续下一封
  │
  ├─→ TypeError (仅 IMAP/OAuth) → 捕获 → 记 error 日志 → 邮件保留 → 继续下一封
  │
  ├─→ ValidationError (附件) → 在 create_object_from_email_message 内捕获
  │                          → 记 error 日志 → 工单已创建 → 邮件删除 → 继续下一封
  │
  └─→ 其他所有异常 (MessageParseError, LookupError, OSError, MemoryError ...)
        ↓
     单封邮件循环未捕获
        ↓
     pop3_sync / imap_sync / imap_oauth_sync / local 处理函数
        ↓
     冒泡到 process_queue()
        ↓
     process_queue() 无外层 try-except
        ↓
     冒泡到 process_email() 的 except Exception as e
        ↓
     记 error 日志 + exc_info 堆栈
        ↓
     该 Queue 的 email_box_last_check 不更新
        ↓
     继续遍历下一个 Queue
```

**唯一的例外**：IMAP/OAuth 的 `server.authenticate()` 失败（`IMAP4.abort`）→ 直接 `sys.exit()` → 整个进程退出，所有 Queue 停摆。

#### 3.6.6 对队列健壮性的影响

| 异常类型 | 单封邮件隔离 | 当前 Queue 停摆 | 其他 Queue 受影响 | 邮件保留/删除 | 可重试 |
|----------|--------------|--------------|----------------|--------------|---------|
| IgnoreTicketException | ✅ | ❌ | ❌ | 保留 | 是（每次都忽略） |
| DeleteIgnoredTicketException | ✅ | ❌ | ❌ | 删除 | 否 |
| TypeError (IMAP/OAuth) | ✅ | ❌ | ❌ | 保留 | 是（每次都失败） |
| MIME 结构损坏 | ❌ | ✅ | ❌ | 保留（损坏的那封及之后的） | 是（无限重试） |
| 编码不匹配（LookupError 等） | ❌ | ✅ | ❌ | 保留 | 是（无限重试） |
| 大附件 ValidationError | ✅（残缺工单） | ❌ | ❌ | 删除 | 否（工单已建，附件丢了） |
| OOM 超大邮件 | ❌ | ✅（进程退出所有 Queue） | ✅（进程退出所有都停摆） | 保留 | 是（每次都 OOM） |
| 磁盘满 OSError | ❌ | ✅ | ❌ | 删除？（取决于异常抛出时机） | 部分 |
| IMAP/OAuth 认证失败 | ❌ | ✅ | ✅（sys.exit 所有都停摆） | 全部保留 | 是 |

**健壮性总结**：
- **最危险的是 MIME 结构损坏：一封坏邮件就能让整个 Queue 的邮件处理卡死
- **最隐蔽的是大附件 ValidationError：邮件被删了，但附件没了，表面上工单是正常的
- **最严重的是 OOM 和 IMAP 认证失败：所有 Queue 全部停摆
- **只有 Ignore/DeleteIgnore 是设计完善的：逐封隔离，互不影响

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

## 8. 异常可发现性与审计痕迹分析

### 8.1 日志体系：可检索的痕迹

#### 8.1.1 每队列独立日志文件

每个 Queue 都有独立的日志配置（`models.py:327-354`）：

```python
# email.py:72-97
logger = logging.getLogger("django.helpdesk.queue." + q.slug)
if q.logging_type in logging_types:
    logger.setLevel(logging_types[q.logging_type])
if q.logging_type in logging_types and q.logging_dir:
    log_file_handler = logging.FileHandler(
        join(q.logging_dir, q.slug + "_get_email.log")
    )
    logger.addHandler(log_file_handler)
```

**日志级别选择**：`none` / `debug` / `info` / `warn` / `error` / `crit`

**日志文件名**：`{logging_dir}/{queue_slug}_get_email.log`

#### 8.1.2 异常事件的日志记录

| 异常类型 | 日志级别 | 代码位置 | 日志内容 |
|----------|----------|----------|----------|
| Queue 整体处理失败 | ERROR + exc_info | `email.py:113` | `"Queue processing failed: {slug} -- {e}"` |
| POP3 STARTTLS 失败 | WARNING | `email.py:139-141` | `"POP3 StartTLS failed..."` |
| 邮件被忽略（保留） | WARNING | `email.py:171-173` 等 | `"Message {n} was ignored..."` |
| 邮件被忽略（删除） | WARNING | `email.py:175-177` 等 | `"Message {n} was ignored and deleted..."` |
| 邮件处理未成功（保留） | WARNING | `email.py:187-189` 等 | `"Message {n} was not successfully processed..."` |
| IMAP STARTTLS 失败 | WARNING | `email.py:200` | `"IMAP4 StartTLS unsupported..."` |
| IMAP 登录失败 | ERROR | `email.py:209-212` | `"IMAP login failed..."` |
| IMAP 搜索失败 | ERROR | `email.py:263-266` | `"IMAP retrieve failed..."` |
| 单封邮件 TypeError | ERROR + exc_info | `email.py:247-249` 等 | `"Unexpected error processing message: {te}"` |
| OAuth 认证失败 | ERROR + exc_info | `email.py:310` | `"IMAP authentication failed in OAUTH: {e1}"` |
| 附件处理失败 | ERROR | `email.py:687` | `str(ValidationError)` |
| 创建 Ticket 成功 | INFO | `email.py:674-681` | `"[queue-id] title"` |
| 附件添加成功 | INFO | `email.py:690-694` | `"Attachment '{name}' ... successfully added..."` |

#### 8.1.3 日志的可检索性

**优点**：
- 每 Queue 独立文件，便于按队列排查
- ERROR 级别有 `exc_info=True`，包含完整堆栈
- 日志级别可配置，生产环境可设为 `error` 减少噪音

**缺点**：
- 无结构化日志（JSON 格式），难以自动化解析
- 无错误码或事件类型标识，只能靠文本匹配
- 日志文件无轮转机制，可能无限增长
- `logging_type=none` 时日志完全静默，无任何痕迹

### 8.2 并发重复工单的可发现性

#### 8.2.1 没有内置检测机制

代码中**没有任何自动检测重复工单的逻辑**：
- `message_id` 字段无 `unique=True` 约束
- 创建 FollowUp 前不检查是否已有相同 `message_id` 的 FollowUp
- 无重复告警或去重逻辑

#### 8.2.2 事后排查手段

**数据库层面**（可行但需手动）：

```sql
-- 查找重复工单（同一发件人+相同标题+相近创建时间）
SELECT id, title, submitter_email, created
FROM helpdesk_ticket
WHERE submitter_email = 'user@example.com'
  AND title = 'Problem with X'
  AND created BETWEEN '2025-01-01 10:00' AND '2025-01-01 10:10';

-- 查找重复 FollowUp（同一 message_id）
SELECT ticket_id, message_id, COUNT(*)
FROM helpdesk_followup
WHERE message_id IS NOT NULL
GROUP BY ticket_id, message_id
HAVING COUNT(*) > 1;
```

**代码层面可用的查询**：

```python
# 按 message_id 查找重复 FollowUp
duplicates = FollowUp.objects.filter(
    message_id__isnull=False
).values(
    'ticket_id', 'message_id'
).annotate(
    cnt=models.Count('id')
).filter(cnt__gt=1)
```

#### 8.2.3 发现难度评估

| 场景 | 自动发现 | 手动排查难度 | 依据 |
|------|----------|-------------|------|
| 同一工单下重复 FollowUp | ❌ 无 | 中 | `message_id` 字段可用于查重 |
| 两张完全重复的工单 | ❌ 无 | 高 | 需通过 submitter_email + title + created 联合判断 |
| 同一邮件产生两个不同 queue 的工单 | ❌ 无 | 极高 | 跨 Queue，更难发现 |

### 8.3 残缺工单的可发现性

#### 8.3.1 日志痕迹

附件处理失败会留下 ERROR 级别日志：

```python
# email.py:683-687
if helpdesk_settings.HELPDESK_ENABLE_ATTACHMENTS:
    try:
        attached = process_attachments(f, files)
    except ValidationError as e:
        logger.error(str(e))
```

**但问题是**：
- 日志中**没有关联 Ticket ID 或 FollowUp ID**
- 只有 `ValidationError` 的消息内容
- 无法直接从日志定位到具体哪张工单出了问题

#### 8.3.2 数据库审计痕迹

残缺工单在数据库中**没有任何状态标记**：
- `Ticket.status` = OPEN（正常状态）
- 无 `attachment_error`、`incomplete` 之类的标记字段
- `Ticket.modified` 会被正常更新（因为 FollowUp.save() 触发）

**可行的事后查询**：

```sql
-- 找出有 message_id 但无附件的 FollowUp（可能是残缺工单，也可能是邮件本身无附件）
SELECT f.id, f.ticket_id, f.message_id, f.date
FROM helpdesk_followup f
WHERE f.message_id IS NOT NULL
  AND f.public = TRUE
  AND f.user_id IS NULL  -- 由邮件创建
  AND NOT EXISTS (
    SELECT 1 FROM helpdesk_followupattachment fa
    WHERE fa.followup_id = f.id
  );
```

但这个查询会匹配到大量"邮件本身就没有附件"的正常工单，误报率很高。

#### 8.3.3 其他痕迹

| 痕迹来源 | 是否存在 | 可追溯性 | 说明 |
|----------|----------|----------|------|
| 日志文件 | ✅ | 弱 | 只有错误文本，无法关联工单 ID |
| FollowUp.message_id | ✅ | 中 | 可用于和邮件服务器对账，但需手动 |
| Ticket.modified | ✅ | 弱 | 无法区分"正常创建"和"残缺创建" |
| Ticket.description | ✅ | 弱 | 正文完整，无法判断附件是否缺失 |
| Webhook 通知 | ✅ | 中 | 如果外部系统记录了 webhook 次数，可发现重复 |
| 通知邮件 | ✅ | 弱 | 发件人收到两封相同通知可能会反馈，但不可靠 |

### 8.4 Webhook：外部审计补充

`webhooks.py` 中 `new_ticket_done` 和 `update_ticket_done` 信号会触发 webhook：

```python
# webhooks.py:39-41
@receiver(update_ticket_done)
def notify_followup_webhooks_receiver(sender, followup, **kwargs):
    notify_followup_webhooks(followup)
```

**如果外部系统接入了 webhook**：
- 可以作为工单创建/更新的外部审计日志
- 重复工单会触发两次 webhook，可能被外部系统检测到
- webhook 数据包含完整的 Ticket 序列化信息

**但 webhook 本身也有问题**：
- 无重试机制（`requests.post` 失败只记日志）
- 无消息队列缓冲，可能丢失
- 发送在 `return ticket` 之前，属于同步调用，可能延迟邮件处理

### 8.5 TicketChange：仅用于人工操作

`TicketChange` 模型（`models.py:1151-1196`）记录字段变更历史，但**邮件创建工单不会产生 TicketChange 记录**：

- `TicketChange` 只在 `views/staff.py:1466` 中人工编辑工单时创建
- 邮件自动化创建的工单没有变更历史
- 无法通过 TicketChange 追溯邮件来源的工单

### 8.6 对账可行性总结

| 对账目标 | 现有能力 | 所需手动工作 |
|----------|----------|-------------|
| 邮件服务器邮件数 vs 创建工单数 | ❌ 无直接支持 | 需人工对比邮件头和 message_id |
| 检测重复工单 | ❌ 无内置机制 | 需定期运行自定义 SQL 查询 |
| 检测残缺工单（附件缺失） | ❌ 无内置机制 | 需结合日志 + 数据库查询，误报率高 |
| 追溯工单来源（哪封邮件） | ⚠️ 部分支持 | FollowUp.message_id 可关联，但仅邮件创建的才有 |
| 拉取成功/失败历史 | ⚠️ 部分支持 | 日志中有记录，Queue.email_box_last_check 只有最后一次 |
| 附件处理成功/失败历史 | ⚠️ 部分支持 | 日志中有 error 记录，但无工单 ID 关联 |

**结论**：代码中留下了一些审计痕迹（日志、message_id 字段、webhook），但都是零散的、非结构化的。没有专门的对账机制或异常工单标记。运维如果不主动监控日志和定期查询数据库，并发重复和残缺工单很难被及时发现。

---

## 9. 流程时序图

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

## 10. 配置项速查

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
