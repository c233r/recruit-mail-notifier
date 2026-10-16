# -*- coding: utf-8 -*-
"""OpenAI 兼容接口的大模型客户端。

刻意只用标准库 urllib，不引入 openai/requests 版本依赖，
这样换任何兼容服务（DeepSeek / OpenAI / 通义 / 本地 vLLM）都只需改 model.ini。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .config import ClassifierConfig, ModelConfig

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT = """你是一个招聘邮件信息抽取助手。用户会给你一封邮件的主题、发件人和正文，
请你判断它是不是「求职相关的通知」，并按固定格式把系统需要的字段抽出来。

只输出一个 JSON 对象，不要输出解释文字，不要加 markdown 代码块。字段固定如下：

{"category": "interview", "is_job_notification": true, "company": "", "position": "",
 "event_time_text": "", "event_time": "", "deadline_text": "", "deadline": "",
 "location": "", "summary": "", "confidence": 0.0, "reason": ""}

各字段含义：
- category            只能是 interview / written_test / offer / rejection / other / unknown 之一
                        interview    面试邀请、面试安排、面试时间地点通知
                        written_test 笔试、在线测评、机试、编程测试邀请
                        offer        录用通知、offer、入职通知
                        rejection    感谢信、未通过通知
                        other        与求职有关但不是上面几类（宣讲会、投递成功回执等）
                        unknown      与求职无关（广告、验证码、账单、社交与购物通知等）
- is_job_notification 这封邮件是否与你的求职有关（true / false）
- company             招聘方公司名，没有就空字符串。不要把招聘平台（智联、牛客、北森、
                      Moka、前程无忧等）或邮箱服务商当成公司
- position            岗位名称，没有就空字符串
- event_time_text     邮件里提到的面试/笔试/测评的具体时间，原样摘录；没有就空字符串
- event_time          上面那个时间换算成带时区偏移的 ISO 8601，例：2026-10-08T15:26:00+08:00
- deadline_text       邮件里提到的截止时间 / 最晚完成时间，原样摘录；没有就空字符串
- deadline            上面那个截止时间换算成 ISO 8601，格式同上
- location            面试或笔试的地点、或线上形式（视频面试/腾讯会议等）；没有就空字符串
- summary             一句话摘要，不超过 60 字，突出「什么时间、在哪里、要做什么」
- confidence          你对 category 判断的把握，0.0 到 1.0；不确定就给低于 0.55 的分
- reason              一句话说明判断依据

严格要求：
1. 投递成功回执、宣讲会通知、校招启动公告、满意度问卷、防诈骗提醒 —— 都不是面试/笔试通知，
   请归为 other 或 unknown，并把 is_job_notification 设为 false。
2. event_time_text 和 deadline_text 必须是邮件原文里出现的片段，不要改写；
   邮件里没写就留空字符串。
3. event_time 和 deadline 要由你推算出来，基准是用户给你的「邮件收到时间」：
   - 邮件写绝对时间（「10月8日 15:26」「2026-10-08」）就直接换算；
   - 邮件写相对说法（「48小时内」「3个工作日内」「下周五前」「收到本邮件后7天内」）
     就从收到时间往后推算；
   - 必须带时区偏移，偏移量用邮件收到时间的那个；
   - 算不出来、或邮件里根本没提，就留空字符串 —— 不要猜，不要编。
4. company 只填真正的招聘方公司名。
5. 所有字段都必须出现，没有内容就用空字符串。
"""

@dataclass
class LlmResult:
    """模型按 JSON 契约返回的字段。"""

    ok: bool = False
    category: str = "unknown"
    is_job_notification: bool = False
    company: str = ""
    position: str = ""
    location: str = ""
    event_time_text: str = ""
    deadline_text: str = ""
    event_time: str = ""   # 模型推算出的 ISO 8601（能处理「48小时内」这类相对说法）
    deadline: str = ""
    summary: str = ""
    notify_time: str = ""   # 兼容旧字段名
    confidence: float = 0.0
    reason: str = ""
    raw: str = ""
    error: str = ""


class LlmError(RuntimeError):
    """模型调用失败。"""


class LlmClient:
    """一次 classify() 对应一次模型调用（含重试）。配额由调用方控制。"""

    def __init__(
        self,
        model_cfg: ModelConfig,
        classifier_cfg: ClassifierConfig,
        categories: Sequence[str],
        logger: Any = None,
    ) -> None:
        self.cfg = model_cfg
        self.classifier_cfg = classifier_cfg
        self.categories = list(categories)
        self.logger = logger

    # ------------------------------------------------------------------ HTTP
    def _post_chat(self, messages: List[Dict[str, str]]) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        if self.cfg.response_format_json:
            payload["response_format"] = {"type": "json_object"}

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
                "User-Agent": "recruit-mail-notifier/0.1",
            },
        )

        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, self.cfg.max_retries) + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.cfg.timeout_seconds) as response:
                    return response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                last_error = LlmError(f"HTTP {exc.code} {exc.reason} {detail}")
            except Exception as exc:
                last_error = LlmError(str(exc))
            if attempt < max(1, self.cfg.max_retries):
                sleep_seconds = min(8, 2 ** (attempt - 1))
                if self.logger:
                    self.logger.warning("模型调用失败（第 %d 次），%d 秒后重试：%s",
                                        attempt, sleep_seconds, last_error)
                time.sleep(sleep_seconds)
        raise last_error or LlmError("模型调用失败")

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _extract_json(content: str) -> Optional[Dict[str, Any]]:
        content = (content or "").strip()
        if not content:
            return None
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
        block = _JSON_BLOCK_RE.search(content)
        if block:
            try:
                parsed = json.loads(block.group(1).strip())
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
        start, end = content.find("{"), content.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(content[start:end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    # ------------------------------------------------------------------ 主入口
    def classify(
        self,
        subject: str,
        sender: str,
        body_text: str,
        received_local: str = "",
        tz_name: str = "",
    ) -> LlmResult:
        if not self.cfg.is_ready():
            return LlmResult(ok=False, error="模型未启用或配置不完整（缺少 base_url/model/api_key）")

        body = (body_text or "")[: self.classifier_cfg.max_body_chars]
        received_line = ""
        if received_local:
            received_line = "邮件收到时间：%s%s\n" % (
                received_local,
                ("（时区 %s）" % tz_name) if tz_name else "",
            )
        user_prompt = (
            f"{received_line}"
            f"发件人：{sender or '（未知）'}\n"
            f"主题：{subject or '（无主题）'}\n"
            f"正文：\n{body}\n"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            response_body = self._post_chat(messages)
        except Exception as exc:
            if self.logger:
                self.logger.error("模型分类失败：%s", exc)
            return LlmResult(ok=False, error=str(exc))

        try:
            payload = json.loads(response_body)
            content = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            return LlmResult(ok=False, error=f"模型响应格式异常：{exc}", raw=response_body[:500])

        parsed = self._extract_json(content)
        if parsed is None:
            return LlmResult(ok=False, error="模型未返回可解析的 JSON", raw=(content or "")[:500])

        def text_of(key: str) -> str:
            return str(parsed.get(key) or "").strip()

        category = text_of("category").lower() or "unknown"
        if category not in self.categories:
            category = "unknown"

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        raw_flag = parsed.get("is_job_notification")
        if isinstance(raw_flag, str):
            is_notification = raw_flag.strip().lower() in {"1", "true", "yes", "y", "是"}
        elif raw_flag is None:
            is_notification = category in ("interview", "written_test", "offer", "rejection")
        else:
            is_notification = bool(raw_flag)

        return LlmResult(
            ok=True,
            category=category,
            is_job_notification=is_notification,
            company=text_of("company"),
            position=text_of("position"),
            location=text_of("location"),
            event_time_text=text_of("event_time_text") or text_of("notify_time"),
            deadline_text=text_of("deadline_text"),
            event_time=text_of("event_time"),
            deadline=text_of("deadline"),
            summary=text_of("summary"),
            notify_time=text_of("notify_time"),
            confidence=confidence,
            reason=text_of("reason"),
            raw=(content or "")[:800],
        )
