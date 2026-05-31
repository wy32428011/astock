"""OpenAI 兼容本地大模型客户端。"""

from __future__ import annotations

import json
import re
from typing import Any

import requests


class LLMClient:
    """调用 OpenAI Chat Completions 兼容接口。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: int,
    ) -> None:
        """初始化接口地址、模型名称和认证信息。"""

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 4096) -> str:
        """发送 Chat Completions 请求并返回文本内容。"""

        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": max_tokens,
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"LLM 请求失败: HTTP {response.status_code}, {response.text[:500]}"
            )

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("LLM 响应缺少 choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise RuntimeError("LLM 响应内容为空")
        return str(content)

    def chat_json(self, messages: list[dict[str, str]], max_tokens: int = 4096) -> Any:
        """调用模型并解析 JSON 响应。"""

        content = self.chat(messages, max_tokens=max_tokens)
        return parse_json_content(content)


def parse_json_content(content: str) -> Any:
    """从模型文本中提取 JSON 对象或数组。"""

    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(1))
