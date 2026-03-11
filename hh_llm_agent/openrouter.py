from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from .config import OpenRouterConfig


class OpenRouterError(Exception):
    pass


@dataclass(frozen=True)
class LLMReply:
    content: str
    reasoning_details: list[dict[str, Any]] | None = None
    parsed: dict[str, Any] | None = None


def extract_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise OpenRouterError("Model did not return JSON.")

    try:
        return json.loads(content[start : end + 1])
    except json.JSONDecodeError as ex:
        raise OpenRouterError("Model returned invalid JSON.") from ex


class OpenRouterChatClient:
    def __init__(
        self,
        config: OpenRouterConfig,
        client: OpenAI | None = None,
    ):
        self.config = config
        self.client = client or OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            default_headers={
                "HTTP-Referer": config.referer,
                "X-Title": config.app_name,
            },
        )

    def _normalize_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        return str(content).strip()

    def _dump_reasoning_details(
        self, details: Any
    ) -> list[dict[str, Any]] | None:
        if not details:
            return None
        dumped = []
        for item in details:
            if hasattr(item, "model_dump"):
                dumped.append(item.model_dump(exclude_none=True))
            elif isinstance(item, dict):
                dumped.append(item)
            else:
                dumped.append({"value": str(item)})
        return dumped

    def _create(self, messages: list[dict[str, Any]]) -> LLMReply:
        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=self.config.temperature,
                max_completion_tokens=self.config.max_completion_tokens,
                extra_body={
                    "reasoning": {"enabled": self.config.reasoning_enabled}
                },
            )
        except Exception as ex:
            raise OpenRouterError(f"OpenRouter request failed: {ex}") from ex
        message = response.choices[0].message
        return LLMReply(
            content=self._normalize_content(message.content),
            reasoning_details=self._dump_reasoning_details(
                getattr(message, "reasoning_details", None)
            ),
        )

    def complete_json(self, messages: list[dict[str, Any]]) -> LLMReply:
        reply = self._create(messages)
        try:
            return LLMReply(
                content=reply.content,
                reasoning_details=reply.reasoning_details,
                parsed=extract_json_object(reply.content),
            )
        except OpenRouterError:
            repair_messages = list(messages)
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": reply.content,
            }
            if reply.reasoning_details:
                assistant_message["reasoning_details"] = reply.reasoning_details
            repair_messages.append(assistant_message)
            repair_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Верни тот же ответ строго как JSON-объект без ``` и "
                        'без пояснений. Формат: {"action": "reply"|"skip", '
                        '"reply_text": "...", "reason": "..."}.'
                    ),
                }
            )
            repaired = self._create(repair_messages)
            return LLMReply(
                content=repaired.content,
                reasoning_details=repaired.reasoning_details,
                parsed=extract_json_object(repaired.content),
            )
