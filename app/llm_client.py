"""LLM 传输层：OpenAI 兼容的 /chat/completions，支持一次性返回和真流式。

只管和模型通信；提示词和上下文在 app/ai.py。
推理模型（kimi-k3 等）先输出思考过程（reasoning_content），它也计入 max_tokens，
所以各任务的 max_tokens 要给思考留余量。
"""
import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("aram_coach.llm")

# 各任务的 max_tokens（含思考过程），可在 config.json 的 llm.max_tokens 覆盖
DEFAULT_MAX_TOKENS = {"ask": 4000, "deep": 6000, "augment": 2000}
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRIES = 3


class LLMError(Exception):
    """给用户看的错误信息。"""


def default_extra_body(base_url: str) -> Dict[str, Any]:
    # reasoning_effort 是月之暗面推理模型支持的参数；别的服务商不一定认，默认不发
    return {"reasoning_effort": "low"} if "moonshot" in (base_url or "") else {}


class LLMClient:
    def __init__(self, config: Dict[str, Any]):
        self.base_url = config.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "qwen-plus")
        self.timeout = config.get("timeout", 60.0)
        self.temperature = config.get("temperature", 0.7)
        self.extra_body: Dict[str, Any] = config.get("extra_body", default_extra_body(self.base_url))
        self.max_tokens = {**DEFAULT_MAX_TOKENS, **(config.get("max_tokens") or {})}
        self._client: Optional[httpx.AsyncClient] = None
        self.transport: Optional[httpx.AsyncBaseTransport] = None  # 测试注入

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0), transport=self.transport)
        return self._client

    def _request(self, messages: List[Dict[str, str]], task: str, stream: bool) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens.get(task, 4000),
            **self.extra_body,
        }
        return url, headers, payload

    @staticmethod
    def _http_error(status: int, text: str) -> LLMError:
        if status == 401:
            return LLMError("❌ API Key 无效或已过期（401），检查 config 里的 llm.api_key")
        if status == 429:
            return LLMError("⏳ 接口限流（429），稍等再试")
        return LLMError(f"❌ 接口错误 {status}：{text[:160]}")

    async def chat(self, messages: List[Dict[str, str]], task: str) -> str:
        """一次性返回正文。429 / 5xx 最多重试 3 次。"""
        if not self.enabled:
            raise LLMError("⚠️ 未配置 LLM API Key")
        url, headers, payload = self._request(messages, task, stream=False)
        for attempt in range(RETRIES):
            try:
                resp = await self._http().post(url, json=payload, headers=headers)
            except httpx.TimeoutException:
                raise LLMError("⏱️ 请求超时，网络可能有问题")
            except httpx.HTTPError as e:
                raise LLMError(f"❌ 连接失败：{type(e).__name__}")
            if resp.status_code in RETRY_STATUS and attempt < RETRIES - 1:
                log.warning("LLM %s，%ds 后重试", resp.status_code, attempt + 1)
                await asyncio.sleep(attempt + 1)
                continue
            if resp.status_code != 200:
                raise self._http_error(resp.status_code, resp.text)
            choice = (resp.json().get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            if not content and choice.get("finish_reason") == "length":
                raise LLMError("⚠️ 模型思考用完了长度上限，没来得及给结论，请重试")
            return content
        raise LLMError("⏳ 接口繁忙，请稍后再试")

    async def stream(self, messages: List[Dict[str, str]], task: str) -> AsyncIterator[Tuple[str, str]]:
        """真流式：依次产出 ("reasoning", 片段) / ("content", 片段)。

        还没收到任何数据前遇到 429 / 5xx 会重试；开始输出后出错直接抛 LLMError。
        调用方中途放弃（比如浏览器断开）时，连接随之关闭，上游也就停止生成。
        """
        if not self.enabled:
            raise LLMError("⚠️ 未配置 LLM API Key")
        url, headers, payload = self._request(messages, task, stream=True)
        for attempt in range(RETRIES):
            got_content = False
            finish = None
            try:
                async with self._http().stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        text = (await resp.aread()).decode("utf-8", "replace")
                        if resp.status_code in RETRY_STATUS and attempt < RETRIES - 1:
                            log.warning("LLM %s，%ds 后重试", resp.status_code, attempt + 1)
                            await asyncio.sleep(attempt + 1)
                            continue
                        raise self._http_error(resp.status_code, text)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if chunk.get("error"):
                            raise LLMError(f"❌ 模型返回错误：{str(chunk['error'])[:160]}")
                        choice = (chunk.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):
                            yield "reasoning", delta["reasoning_content"]
                        if delta.get("content"):
                            got_content = True
                            yield "content", delta["content"]
                        finish = choice.get("finish_reason") or finish
            except httpx.TimeoutException:
                raise LLMError("⏱️ 请求超时，网络可能有问题")
            except httpx.HTTPError as e:
                raise LLMError(f"❌ 连接失败：{type(e).__name__}")
            if not got_content and finish == "length":
                raise LLMError("⚠️ 模型思考用完了长度上限，没来得及给结论，请重试")
            return
        raise LLMError("⏳ 接口繁忙，请稍后再试")

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
