# 招聘邮件智能通知系统

> 一个「零第三方依赖也能跑」的个人求职邮件助手：自动从邮箱里识别出**面试 / 笔试 / 测评通知**，抽取公司、岗位、时间、地点，跨天追踪未完成项，每天汇总成一封邮件推送；你直接**回复那封汇总邮件**就能标记完成，系统第二天只推未完成的和新的。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-stdlib--only-success)](#技术栈)
[![Tests](https://img.shields.io/badge/tests-119%20passing-brightgreen)](#测试)
[![License](https://img.shields.io/badge/license-MIT-blue)](#许可证)

---

## 目录

- [为什么做这个](#为什么做这个)
- [功能特性](#功能特性)
- [系统架构](#系统架构)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [命令一览](#命令一览)
- [核心设计](#核心设计)
- [测试](#测试)
- [开机自启](#开机自启)
- [常见问题](#常见问题)
- [技术栈](#技术栈)
- [已知限制](#已知限制)
- [许可证](#许可证)

---

## 为什么做这个

秋招期间邮箱会同时涌入几百封邮件：宣讲会邀请、投递成功回执、防诈骗提醒、购物账单……真正重要的**面试 / 笔试通知**很容易被淹掉。而漏掉一封笔试邀请的代价是直接失去一个机会。

人工每天翻邮箱的问题是：

1. **重复劳动** —— 每天都要把同样的几十封邮件扫一遍，判断哪些是新的、哪些已经处理过；
2. **容易看漏** —— 通知混在广告和回执里，关键信息（截止时间、考试链接）藏在长正文中间；
3. **时间记不住** —— 「48 小时内完成测评」「下周五前确认」这类**相对时间**，光看邮件根本算不出还剩多久；
4. **没有状态** —— 哪些笔试已经做完了、哪些还欠着，全靠脑子记。

本项目把这一整套流程自动化：**收信 → 识别 → 抽取结构化信息 → 落库 → 每天汇总推送 → 回复驱动状态更新**。

---

## 功能特性

### 邮件识别与信息抽取

- **大模型结构化抽取**：把邮件正文交给 LLM，按固定 JSON 契约返回类别、公司、岗位、时间、地点、摘要等字段，不依赖脆弱的正则规则
- **相对时间推理**：告诉模型「邮件收到时间」，让它自己推算 `48小时内`、`3个工作日内`、`下周五` 这类相对说法，输出带时区的 ISO 8601 时间；算不出来就留空，**不编造**
- **幻觉校验**：模型给的时间要能解析、要落在合理区间内，否则丢弃并退回正则归一化兜底
- **三种策略可切换**：`llm_only`（全交给模型）/ `rules_then_llm`（规则优先、模糊时问模型）/ `rules_only`（纯规则、离线零成本），改配置即时生效

### 追踪与推送

- **UID 增量拉取**：记录上次处理到哪个 UID，每次只拉新邮件，不重复下载
- **不漏信游标**：只有连续成功处理到的 UID 才推进游标；某封处理失败就停在它之前，下一轮重试
- **Message-ID 去重**：同一个 `Message-ID` 只入库一次；缺失时用内容摘要生成伪 ID
- **跨天续推**：今天没标记完成的，明天继续提醒，并标注「第 N 次提醒」
- **变更检测**：自上次推送以来没有新通知、也没有标记完成，就跳过发送 —— 一天开几次电脑只收到一封
- **按截止时间排序**：最紧急的排最前面

### 双向交互（回复邮件驱动状态机）

汇总邮件里每条都带一个 `#编号`，直接回复就能操作：

| 回复内容 | 效果 |
| --- | --- |
| `#12 #15` | 精确标记这两条完成（**推荐**） |
| `#12 已完成` | 同上，「已完成」可有可无 |
| `腾讯 已完成` | 按公司名标记，会把这家公司**所有**未完成项一起标掉 |
| `全部完成` | 一次清空当前所有提醒 |

> 只要回复里写了 `#编号`，就**只按编号匹配**，同一行里的公司名会被忽略 —— 因为同一家公司常常会发多场笔试 / 面试，按公司名匹配会把它们一次性全标掉。

系统会校验回复的发件人是否在白名单内，并切掉引用内容，防止误触。

### 邮箱安全

- **不标已读**：取信统一使用 `BODY.PEEK[]`（而不是 `RFC822`），即使以读写方式打开邮箱也不会把邮件标记为已读
- **不删除、不移动**：默认 `readonly = true`
- **归档只复制**：可选的归档功能用 IMAP `COPY` 把通知复制到专用文件夹，**原邮件仍留在收件箱**
- **凭据不进代码**：邮箱授权码和 API Key 只从环境变量读取，配置文件里只写变量名

### 工程性

- **标准库优先**：`imaplib` / `smtplib` / `sqlite3` / `urllib` / `email` / `zoneinfo`，两个可选依赖不装也能跑
- **119 个单元测试**，全部离线运行（不联网、不调模型），用假 IMAP / 假 SMTP / 假 LLM 覆盖全流程
- **配置热加载**：两个 INI 每次运行重新读盘，改完保存即生效
- **时区降级**：没装 `tzdata` 时自动退回固定偏移实现，不会崩

---

## 系统架构

### 数据流

```
                       ┌──────────────────────────────┐
                       │      IMAP 邮箱（默认只读）      │
                       └───────────────┬──────────────┘
                                       │ UID 增量拉取（BODY.PEEK[]）
                                       ▼
  ┌────────────────┐          ┌─────────────────┐          ┌───────────────┐
  │  完成回复识别    │◀─────────│    Pipeline     │─────────▶│ SQLite 数据库  │
  │  replies.py    │          │  pipeline.py    │          │   db.py       │
  └───────┬────────┘          └────────┬────────┘          └───────┬───────┘
          │ 标记完成 / 撤销             │ 分类抽取                   │
          │                    ┌───────▼────────┐                  │
          │                    │   LLM 分类器    │                  │
          │                    │ llm_client.py  │                  │
          │                    └───────┬────────┘                  │
          │                            │ JSON                      │
          ▼                            ▼                           ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │                    汇总邮件生成器  notifier.py                         │
  │  按截止时间排序 · 显示 #编号 · 「不限时」兜底 · 纯文本 + HTML 双版本       │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  │ SMTP
                                  ▼
                        用户邮箱（每天一封汇总）
                                  │
                                  │ 用户回复「#12 #15」
                                  ▼
                          下一轮收信时被识别，闭环
```

### 模块职责

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 命令行入口，15 个子命令，统一日志与配置装配 |
| `config.py` | 读取 `mail.ini` + `model.ini` + `.env`，构建配置对象；时区解析与降级 |
| `mail_reader.py` | IMAP 连接、UID 增量搜索、批量取信、MIME 解析、归档 `COPY` |
| `mime_utils.py` | MIME 头解码、HTML 转纯文本、摘要生成、伪 Message-ID |
| `classifier.py` | 分类编排：策略选择、规则匹配、时间归一化、`_sane_iso` 校验 |
| `llm_client.py` | OpenAI 兼容接口客户端，JSON 契约提示词、重试、解析 |
| `replies.py` | 完成回复解析：编号 / 公司名提取、引用切分、状态分类 |
| `pipeline.py` | 业务编排：`run_poll` / `run_summary` / `run_once` / 归档 |
| `db.py` | SQLite 表结构、迁移、游标、去重、状态机、变更检测 |
| `notifier.py` | 汇总邮件渲染（文本 + HTML）与 SMTP 发送（含重试） |
| `scheduler.py` | `run --watch` 常驻模式下的定时调度 |
| `logging_setup.py` | 日志配置，无控制台时自动跳过 StreamHandler |

---

## 目录结构

```
recruit-mail-notifier/
├── main.py                        # 入口：python main.py <命令>
├── requirements.txt               # 可选依赖（不装也能跑）
├── start_daemon.cmd               # Windows 开机自启调用的启动脚本
├── .env.example                   # 环境变量样例（复制成 .env）
├── .gitignore
│
├── config/
│   ├── mail.ini                   # 邮件配置：IMAP / SMTP / 推送策略 / 保留期
│   ├── model.ini                  # 模型配置：策略 / 接口 / 成本保护
│   └── rules.json                 # 规则词表：类别关键词、否定词、公司后缀
│
├── recruit_mail_notifier/         # 主包
│   ├── cli.py
│   ├── config.py
│   ├── db.py
│   ├── mail_reader.py
│   ├── mime_utils.py
│   ├── classifier.py
│   ├── llm_client.py
│   ├── replies.py
│   ├── pipeline.py
│   ├── notifier.py
│   ├── scheduler.py
│   └── logging_setup.py
│
├── tools/
│   └── install_autostart.ps1      # 注册 / 启用 / 停用 / 删除计划任务
│
├── tests/                         # 119 个单元测试，全部离线
│   ├── fixtures/*.eml             # 11 封样本邮件
│   ├── sample_emails.py           # 构造内存邮件的辅助函数
│   ├── sample_replies.py
│   └── test_*.py
│
├── data/                          # 运行时生成（已 gitignore）
└── logs/                          # 运行时生成（已 gitignore）
```

---

## 快速开始

### 环境要求

- **Python 3.10+**
- 一个支持 IMAP / SMTP 的邮箱（QQ、163、Gmail 均可）
- 可选：一个大模型 API Key（DeepSeek / OpenAI / 通义 / 本地 vLLM 等任何 OpenAI 兼容服务）

### 1. 安装

```bash
git clone https://github.com/<your-name>/recruit-mail-notifier.git
cd recruit-mail-notifier

# 依赖是可选的，装不装都能跑
pip install -r requirements.txt
```

### 2. 配置凭据

```bash
cp .env.example .env
```

编辑 `.env`，填入你的邮箱和模型凭据：

```ini
RECRUIT_IMAP_USER=your_account@qq.com
RECRUIT_IMAP_PASSWORD=你的IMAP授权码      # 注意不是网页登录密码
RECRUIT_SMTP_USER=your_account@qq.com
RECRUIT_SMTP_PASSWORD=你的SMTP授权码
RECRUIT_LLM_API_KEY=sk-xxxxxxxx
```

> **`.env` 已被 `.gitignore` 忽略，不会被提交。** 代码里不会出现任何账号密码，只按 `mail.ini` / `model.ini` 里配置的**变量名**去读。

各邮箱的授权码获取方式：

| 邮箱 | 位置 |
| --- | --- |
| QQ 邮箱 | 设置 → 账户 → POP3/IMAP/SMTP 服务 → 生成授权码 |
| 163 邮箱 | 设置 → POP3/SMTP/IMAP → 开启并获取授权码 |
| Gmail | 需要开启两步验证后生成「应用专用密码」 |

### 3. 修改邮件配置

编辑 `config/mail.ini`，至少改这几项：

```ini
[imap]
host = imap.qq.com              # 换成你的邮箱服务商
username =                      # 留空则从 RECRUIT_IMAP_USER 读

[notify]
recipients = your_account@qq.com   # 汇总邮件发给谁
timezone = Asia/Shanghai
```

### 4. 检查配置

```bash
python main.py check      # 不联网，只检查配置是否完整
python main.py init-db    # 建表
```

### 5. 第一次运行

```bash
python main.py run
```

它会：**拉取上次时间点之后的新邮件 → 分类入库 → 处理你的完成回复 → 有变化才发汇总 → 退出**。

想先看看会发生什么、又不想真的发信：

```bash
python main.py run --dry-run      # 汇总写到 logs/outbox/，不发信
```

---

## 配置说明

配置拆成**两个 INI**，因为它们的调整频率完全不同：邮件设置你可能天天改，模型设置基本定下来就不动。

两个文件都是**每次运行重新读盘**（热加载），改完保存即生效，不需要重启。

### `config/mail.ini` —— 邮件相关

| 段落 | 关键项 | 说明 |
| --- | --- | --- |
| `[imap]` | `host` / `port` / `use_ssl` | 收信服务器 |
| | `username` / `user_env` | 账号；留空则从环境变量读 |
| | `readonly` | 默认 `true`，永不标已读 / 删除 / 移动 |
| | `fetch_mode` | `uid`（推荐，精确增量）或 `since`（按时间重捞） |
| | `initial_lookback_days` | 首次运行只回看最近 N 天 |
| | `archive_folder` | 把通知**复制**到专用文件夹；留空 = 不归档 |
| | `webmail_url_template` | 生成「点开原邮件」的链接模板，见下文 |
| `[smtp]` | `host` / `port` / `from_name` | 发信服务器与显示名 |
| `[notify]` | `recipients` | 汇总收件人 |
| | `timezone` | 决定日期显示与「每天 10:00」按哪个时区算 |
| | `categories` | 哪些类别算「相关」：`interview,written_test` |
| | `push_only_when_changed` | 默认 `true`，无变化不打扰 |
| | `reply_completion_enabled` | 是否允许回复邮件标记完成 |
| | `remind_unfinished_daily` | 未完成的是否每天续推 |
| | `sort_by` | `deadline`（按截止时间）或 `received` |
| `[retention]` | `max_age_days` | 只处理最近 N 天到达的邮件（IMAP 服务端过滤） |
| | `purge_after_days` | 清理本地数据库中 N 天前的记录（**不碰邮箱**） |

### `config/model.ini` —— 模型相关

```ini
[classifier]
strategy = llm_only        # llm_only / rules_then_llm / rules_only
pending_review_confidence = 0.55   # 低于此置信度进「待确认」队列
max_body_chars = 6000      # 送模型前正文截断长度

[model]
enabled = true
base_url = https://api.deepseek.com/v1
model = deepseek-chat
api_key_env = RECRUIT_LLM_API_KEY
response_format_json = true
daily_call_limit = 0       # 每天最多调用多少次，0 = 不限制
```

`strategy` 三种模式：

| 值 | 行为 | 适用 |
| --- | --- | --- |
| `llm_only` | 全部交给模型 | 效果最好，推荐 |
| `rules_then_llm` | 主题命中关键词直接判定，否则问模型 | 想省调用费 |
| `rules_only` | 只用规则，不调模型 | 离线演示、零成本 |

### `config/rules.json`

规则模式使用的词表：类别关键词（`笔试` / `测评` / `面试`）、否定词（`未通过` / `已结束` / `感谢您参加`）、公司名后缀等。`llm_only` 模式下不参与。

### 关于「点开原邮件」的链接

`webmail_url_template` 支持这些占位符：`{account}` `{mailbox}` `{uid}` `{message_id}` `{subject}` `{from_addr}` `{date}`。

```ini
# Gmail：按 Message-ID 精确打开
webmail_url_template = https://mail.google.com/mail/u/0/#search/rfc822msgid:{message_id}

# Outlook：按 Message-ID 搜索
webmail_url_template = https://outlook.live.com/mail/0/search?q={message_id}
```

> ⚠️ **QQ 邮箱做不到。** 它的 PC 版用 `?sid=<会话令牌>&mailid=<服务端内部ID>`，手机版用列表**序号**（`#/list/1/1`），搜索页用临时 `searchKey` —— 三者都不是稳定可分享的地址。留空时该字段退化为 `mid:<message-id>` 标识（不可点击，仅用于区分邮件）。QQ 用户建议开启 `archive_folder` 归档，直接在专用文件夹里翻。

---

## 命令一览

```
python main.py <命令> [参数]
```

全局参数：`--dry-run`（不真发信）、`--log-level {DEBUG,INFO,WARNING,ERROR}`

### 日常使用

| 命令 | 说明 |
| --- | --- |
| `run` | **一次执行**：收信 + 分类 + 应用完成回复 + 汇总推送，然后退出。开机自启跑的就是它 |
| `run --watch` | 常驻模式，按 `poll_interval_minutes` 轮询、按 `daily_summary_time` 汇总 |
| `summary --force` | 立刻强制发一封汇总（跳过「无变化就不发」的保护） |
| `poll` | 只收信 + 分类，不发汇总 |

### 查看与维护

| 命令 | 说明 |
| --- | --- |
| `check` | 检查配置是否就绪（不联网） |
| `init-db` | 初始化数据库表 |
| `stats` | 各状态 / 类别 / 来源的统计概览 |
| `jobs` | 按截止时间由近到远列出未完成的面试 / 笔试（`--include-completed` 一并显示已完成） |
| `pending` | 列出待人工确认的低置信度邮件 |
| `list --status <状态> --limit N` | 列出已入库邮件 |
| `replies` | 查看处理过的完成回复（什么时候标记了什么） |
| `classify-eml <文件>` | 离线分析本地 `.eml`，验证分类效果（不联网、不入库） |
| `archive` | 把已入库的招聘通知一次性复制到归档文件夹 |
| `purge [--days N] [--force]` | 清理数据库中超过保留期的记录 |

### 手动标记

| 命令 | 说明 |
| --- | --- |
| `done --id 12 15` | 按编号标记完成（推荐） |
| `done 腾讯` | 按公司名标记（会标掉该公司全部未完成项） |
| `done --all` | 全部标记完成 |
| `undo --id 12` / `undo 腾讯` / `undo --all` | 撤销完成标记 |

> 每个子命令都支持 `--help`，例如 `python main.py jobs --help`。

### 典型工作流

```bash
# 开机登录后自动跑一次（计划任务）
python main.py run

# 收到汇总邮件后，直接回复「#12 #15」标记完成 —— 下次 run 会自动识别处理

# 想主动补一封汇总看看
python main.py summary --force

# 看看还剩哪些没做
python main.py jobs
```

---

## 核心设计

### 1. 不漏信的增量游标

最容易出错的地方是「拉到哪了」。这里的做法是：

- 用 `UID` 而不是序列号（`SEARCH UID N:*`），因为 UID 在文件夹内单调递增且不随删除而变化
  > 踩过的坑：QQ 邮箱对**裸序列号**的 `SEARCH 3238:*` 会返回 `OK` 但结果为空，必须写成 `SEARCH UID 3238:*`
- 同时记录 `UIDVALIDITY`，它变了说明 UID 空间失效，需要重新全量对齐
- **只有连续成功处理到的 UID 才推进游标**。中间某封失败，游标停在它之前，下一轮从那封重新开始 —— 宁可重复处理（有 Message-ID 去重兜底），也不能漏

### 2. 大模型结构化抽取与相对时间推理

提示词要求模型返回固定 JSON：

```json
{
  "category": "written_test",
  "is_job_notification": true,
  "company": "某某科技",
  "position": "后端开发工程师",
  "event_time_text": "",  "event_time": "",
  "deadline_text": "收到本邮件后48小时内", "deadline": "2026-10-03T21:20:00+08:00",
  "location": "线上测评",
  "summary": "在线测评邀请，需在收到邮件后 48 小时内完成。",
  "confidence": 0.95,
  "reason": "邮件明确要求 48 小时内完成测评"
}
```

关键点是**把「邮件收到时间」也告诉模型**，并区分两类字段：

- `*_text` —— 从邮件原文**原样摘录**，不许改写（用于展示与追溯）
- `event_time` / `deadline` —— 由模型**推算**出的带时区 ISO 8601

这样 `48小时内`、`3个工作日内`（要跳过周末）、`下周五 14:30` 这类相对时间才处理得了 —— 实测：

| 正文说法（收信时间 2026-10-01 21:20 +0800） | 模型输出 |
| --- | --- |
| 收到本邮件后**48小时内**完成 | `2026-10-03T21:20:00+08:00` |
| **3个工作日内**确认 | `2026-10-06T21:20:00+08:00` |
| 面试安排在**下周五** 14:30 | `2026-10-09T14:30:00+08:00` |
| 完全不提时间 | `""`（**不编造**） |

模型给的时间还要过 `_sane_iso()` 校验：能被解析、落在合理区间内（前 90 天 ~ 后 400 天），否则丢弃并退回「正则归一化 → 全部留空」的降级链。

### 3. 回复邮件驱动的状态机

状态流转：

```
                    ┌──────────────┐
   新邮件分类完成 ──▶ │     new      │
                    └──────┬───────┘
                           │ 汇总邮件发出
                           ▼
                    ┌──────────────┐   回复「#12」   ┌──────────────┐
                    │   notified   │ ─────────────▶ │     done     │
                    └──────┬───────┘                └──────────────┘
                           │ 置信度不足                     ▲
                    ┌──────▼───────┐                       │ undo
                    │pending_review│───────────────────────┘
                    └──────────────┘
```

- **编号优先**：回复里只要出现 `#编号`，就只按编号匹配，忽略公司名
- **引用切分**：回复里会带上原文引用，必须在 `_QUOTE_CUT_PATTERNS` 处切开，否则引用里的公司名会误触发
- **安全阀**：正文很长、又没有「完成」字眼、也没有明确编号时，判定为引用没切干净，**不动任何记录**
- **发件人白名单**：只有配置里允许的地址发来的回复才算数

### 4. 防打扰的变更检测

时间戳统一用**微秒精度**的 UTC ISO 8601（`%Y-%m-%dT%H:%M:%S.%f+00:00`）。

这不是洁癖：判断「自上次推送以来有没有变化」要比较时间先后，秒级精度会让同一秒内发生的事情（先推送、紧接着标记完成）分不出先后，结果就是改了状态却不会再推送。

`push_only_when_changed = true` 时，只有以下情况才发汇总：

- 有 `created_at` 晚于 `last_push_at` 的未通知邮件
- 有 `updated_at` 晚于 `last_push_at` 且处于活跃状态的记录（覆盖标记完成 / 撤销完成）

### 5. 邮箱只读保证

「只读」这件事做了三层保护：

1. `imap.readonly = true` 时以 IMAP 只读方式 `SELECT`
2. 取信统一用 `BODY.PEEK[]` 而不是 `RFC822` —— 后者会**隐式设置 `\Seen` 标志**，在读写模式下会把你的邮件全变成已读
3. 归档功能需要 `COPY`，而 IMAP 规定 `COPY` 不能作用于只读邮箱，所以开启归档时会以读写方式打开 —— 但因为用了 `BODY.PEEK[]`，**依然不会标已读**

> 实测：归档 52 封邮件后，收件箱未读数保持 1888 不变。

### 6. 中文文件夹名的 IMAP 编码

IMAP 协议规定邮箱名要用 **modified UTF-7**（RFC 3501 §5.1.3）编码，而 `imaplib` 默认以 ASCII 编码命令参数 —— 直接把「招聘通知」传给 `CREATE` 会抛 `UnicodeEncodeError`。`encode_imap_utf7()` 负责这层转码：

```python
encode_imap_utf7("招聘通知")   # -> '&YtuAWJAad+U-'
encode_imap_utf7("INBOX")     # -> 'INBOX'
encode_imap_utf7("A&B")       # -> 'A&-B'
```

### 7. Windows 开机自启的坑

`start_daemon.cmd` 里有几个不太直观但必须的处理：

- **`start /wait`**：`pythonw.exe` 是 GUI 子系统程序，`cmd` 不会等它结束就往下走，计划任务会因此误判结果（实测退出码变成 `0xC000013A`）并触发无谓的重启
- **纯 ASCII**：`chcp 65001` 写在批处理里、文件又含中文时，`cmd.exe` 会按字节偏移重读文件导致解析错位，把注释后半段当成命令执行（弹「Windows 找不到文件」）
- **失败重试**：登录瞬间网络往往没起来，脚本检查退出码，最多重试 3 次、间隔 3 分钟
- **`ping` 代替 `timeout`**：`timeout` 在没有控制台 / stdin 时会报错

---

## 测试

```bash
python -m unittest discover -s tests -t .
```

**119 个测试，全部离线运行**（不联网、不调用大模型），覆盖：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_mime_utils.py` | MIME 头解码、HTML 转文本、摘要生成 |
| `test_classifier.py` | 时间归一化、年份翻转、异常时间过滤 |
| `test_subject_rules.py` | 主题规则匹配、否定词 |
| `test_llm_only.py` | llm_only 策略、模型降级 |
| `test_replies.py` | 完成回复解析、编号匹配、引用切分 |
| `test_db.py` | 表结构、迁移、去重、状态机、变更检测 |
| `test_carryover.py` | 跨天续推、按编号 vs 按公司名标记 |
| `test_pipeline_offline.py` | 端到端流程（假 IMAP + 假 SMTP） |
| `test_run_once.py` | `run` 一次性执行语义 |
| `test_retention.py` | 保留期过滤、清理策略 |
| `test_archive.py` | 归档文件夹、只读策略、UTF-7 编码 |

测试用**假 IMAP / 假 SMTP / 假 LLM**替换外部依赖，所以既跑得快，也不会污染真实邮箱。

---

## 开机自启

提供 Windows 计划任务方案（登录后延迟 10 分钟运行一次）：

```bat
:: 注册并启用（不需要管理员权限）
tools\install_autostart.cmd

:: 查看状态
tools\install_autostart.cmd -Status

:: 临时停用 / 重新启用 / 彻底删除
tools\install_autostart.cmd -Disable
tools\install_autostart.cmd -Enable
tools\install_autostart.cmd -Remove

:: 自定义启动延迟（分钟，默认 10）
tools\install_autostart.cmd -DelayMinutes 20
```

> **为什么用 `.cmd` 而不是直接跑 `.ps1`？**
> Windows 默认禁止运行 PowerShell 脚本。别人 clone 下来直接跑 `.ps1` 会报
> 「**在此系统上禁止运行脚本**」或「**未对文件进行数字签名**」。
> `install_autostart.cmd` 只是用 `-ExecutionPolicy Bypass` 转发一次调用 ——
> **只对这一个进程生效，不会修改系统的任何安全设置**。如果你更习惯手动调用：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Status
> ```

注册后的任务：

| 项 | 值 |
| --- | --- |
| 触发器 | 登录时，延迟 10 分钟（给网络留出启动时间） |
| 多实例策略 | `IgnoreNew`（永远不会跑出两个进程） |
| 执行时限 | 无限制 |
| 启动脚本 | `start_daemon.cmd` |

`start_daemon.cmd` 内部的失败重试 + 计划任务的 `-StartWhenAvailable`（错过时间后补跑）共同保证：**即使登录时断网，也不会白白错过一天**。

Linux / macOS 可以用 `cron`：

```cron
# 每 10 分钟看一眼（实际由 push_only_when_changed 决定发不发）
*/10 * * * * cd /path/to/recruit-mail-notifier && /usr/bin/python3 main.py run >> logs/cron.log 2>&1
```

---

## 常见问题

<details>
<summary><b>为什么收到汇总却说「没有新的面试/笔试通知」？</b></summary>

邮件被分到了 `other` / `unknown`，或者置信度低于 `pending_review_confidence` 进了待确认队列。用 `python main.py pending` 查看，或者用 `python main.py classify-eml 某封邮件.eml` 单独调试分类效果。
</details>

<details>
<summary><b>改了 INI 需要重启吗？</b></summary>

不需要。两个 INI 每次运行都会重新读盘（热加载）。但**环境变量**的改动需要重启进程 —— 环境变量只在进程启动时读取一次。
</details>

<details>
<summary><b>为什么我回复了「已完成」，状态却没变？</b></summary>

按可能性排序：

1. 回复里既没有 `#编号` 也没有公司名 —— 系统不知道该标哪条
2. 回复的账号不在 `reply_allowed_senders` 白名单里（留空时默认用 `recipients` + SMTP 账号）
3. 正文很长、没有「完成」字眼、也没有编号 —— 被安全阀拦下了（防止引用没切干净误标）

详见 `logs/run.log` 里的「收到完成回复」记录，或 `python main.py replies`。
</details>

<details>
<summary><b>会把我邮箱里的邮件标成已读 / 删掉吗？</b></summary>

不会。取信统一用 `BODY.PEEK[]`，默认以只读方式打开邮箱，既不标已读也不删除、不移动。归档功能也只是 `COPY` 一份副本。

唯一会写邮箱的操作是归档（`archive_folder` 非空时），而且默认是关的。
</details>

<details>
<summary><b>数据库删了会怎样？</b></summary>

不会丢邮件 —— 邮件始终在邮箱里。删掉 `data/recruit_mail.db` 后，程序会按 `max_age_days` 和 `initial_lookback_days` 重新拉取并分类，但**之前标记的「已完成」状态会丢失**。

顺带一提：记录编号（`#12` 这种）是数据库自增主键，删库重建后会重新编号，之前邮件里的编号就失效了。
</details>

<details>
<summary><b>支持 Gmail / Outlook 吗？</b></summary>

支持。改 `mail.ini` 里的 `host` / `port`，并在 `.env` 里填对应的账号密码（Gmail 需要「应用专用密码」）。链接模板可以用 README 上面给的现成写法，能得到真正可点击的「打开原邮件」链接。
</details>

<details>
<summary><b>别人跑 <code>tools\install_autostart.ps1</code> 报「禁止运行脚本 / 未对文件进行数字签名」？</b></summary>

那是 Windows 的 PowerShell **执行策略**（`ExecutionPolicy`）在拦脚本，不是脚本本身的问题。Windows 客户端默认就是 `Restricted`，任何 `.ps1` 都不许跑。

**用附带的 `.cmd` 包装器即可** —— 它只用 `-ExecutionPolicy Bypass` 给当前进程放行，**不改系统设置**：

```bat
tools\install_autostart.cmd -Status
```

坚持直接调 `.ps1` 的三种写法：

```powershell
# ① 单次放行（推荐，不落盘）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_autostart.ps1

# ② 只放行当前这个会话
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# ③ 只解开这个文件的「来自 Internet」标记（从 ZIP 解压后常见）
Unblock-File .\tools\install_autostart.ps1
```

⚠️ 不建议为了一个脚本去改全局执行策略（`Set-ExecutionPolicy` 不带 `-Scope` 就是改全局）。
</details>

<details>
<summary><b>模型调用要花多少钱？</b></summary>

只有**新邮件**才会调用模型，去重后的量级是「每天几十次」。用 `deepseek-chat` 量级算，一个月通常不到一块钱。

想完全免费可以设 `model.ini` 的 `strategy = rules_only`，退化为纯规则匹配（准确率会下降）。
</details>

---

## 技术栈

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 语言 | Python 3.10+ | 用到 `zoneinfo`、`dataclasses`、`from __future__ import annotations` |
| 收信 | `imaplib` + `email` | 标准库，SSL 连接，`BODY.PEEK[]` 只读取信 |
| 发信 | `smtplib` | 标准库，SSL + 重试退避 |
| 存储 | `sqlite3` | 标准库，单文件数据库，带迁移机制 |
| 模型 | `urllib.request` | 标准库直连 OpenAI 兼容接口，无 SDK 依赖 |
| 时区 | `zoneinfo` | 缺失 `tzdata` 时自动降级为固定偏移 |
| 测试 | `unittest` | 标准库，假依赖注入，全离线 |

**刻意不用任何第三方库作为硬依赖** —— 这样在任何一台装了 Python 的机器上 `git clone` 完就能跑，对课程演示环境特别友好。

---

## 已知限制

- **QQ 邮箱没有稳定的单封邮件链接** —— 详见上文「关于链接」一节，这是服务商限制，不是实现问题
- **纯文本版汇总没有富文本** —— 需要点击的链接在 HTML 版里才是超链接
- **单用户设计** —— 数据库是单文件 SQLite，没有多租户隔离
- **分类准确率依赖模型** —— `rules_only` 模式在主题没写关键词时容易漏判
- **Windows 自启方案只覆盖 Windows** —— Linux / macOS 用 cron（README 里有示例）

---

## 许可证

[MIT](LICENSE)

---

<p align="center">
  <sub>本项目为个人学习 / 课程设计作品。邮件凭据全部通过环境变量管理，仓库中不含任何真实账号信息。</sub>
</p>
