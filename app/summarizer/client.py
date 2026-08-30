"""OpenAI 兼容 LLM 客户端：DeepSeek / 千问(DashScope compatible-mode) 共用。

构造：LLMClient(api_key, base_url, default_model)。
base_url 示例：https://api.deepseek.com 或 https://dashscope.aliyuncs.com/compatible-mode/v1
chat() 可每次指定 model（不同任务用不同档位，控制成本）。
"""
from __future__ import annotations

import httpx


class LLMClient:
    def __init__(self, api_key: str, base_url: str, default_model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model

    def chat(self, prompt: str, system: str = "你是专业的结构化笔记助手。",
             model: str | None = None, temperature: float = 0.3,
             timeout: float = 120) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self.default_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
        }
        import time as _time

        last_err = None
        for attempt in range(3):
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
                if resp.status_code == 429:
                    _time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < 2:
                    _time.sleep(3 * (attempt + 1))
                else:
                    raise RuntimeError(f"LLM 调用失败(3次): {last_err}") from last_err
        raise RuntimeError(f"LLM 调用失败: {last_err}")
