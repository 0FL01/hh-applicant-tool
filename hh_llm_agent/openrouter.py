from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from .config import OpenRouterConfig


class OpenRouterError(Exception):
    pass


@dataclass(frozen=True)
class StructuredOutputSchema:
    name: str
    schema: dict[str, Any]
    strict: bool = True


@dataclass(frozen=True)
class LLMReply:
    content: str
    reasoning_details: list[dict[str, Any]] | None = None
    parsed: dict[str, Any] | None = None


class OpenRouterChatClient:
    RESPONSE_HEALING_PLUGIN_ID = "response-healing"

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

    def _response_format(
        self,
        schema: StructuredOutputSchema,
    ) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema.name,
                "strict": schema.strict,
                "schema": schema.schema,
            },
        }

    def _create(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: StructuredOutputSchema | None = None,
    ) -> LLMReply:
        extra_body: dict[str, Any] = {
            "reasoning": {"enabled": self.config.reasoning_enabled}
        }
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_completion_tokens": self.config.max_completion_tokens,
            "extra_body": extra_body,
        }
        if schema is not None:
            request["response_format"] = self._response_format(schema)
            extra_body["provider"] = {"require_parameters": True}
            extra_body["plugins"] = [{"id": self.RESPONSE_HEALING_PLUGIN_ID}]
        try:
            response = self.client.chat.completions.create(**request)
        except Exception as ex:
            raise OpenRouterError(f"OpenRouter request failed: {ex}") from ex
        message = response.choices[0].message
        return LLMReply(
            content=self._normalize_content(message.content),
            reasoning_details=self._dump_reasoning_details(
                getattr(message, "reasoning_details", None)
            ),
        )

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: StructuredOutputSchema,
    ) -> LLMReply:
        reply = self._create(messages, schema=schema)
        try:
            parsed = json.loads(reply.content)
        except json.JSONDecodeError as ex:
            raise OpenRouterError(
                "Model returned invalid structured JSON."
            ) from ex
        if not isinstance(parsed, dict):
            raise OpenRouterError("Model returned non-object structured JSON.")
        return LLMReply(
            content=reply.content,
            reasoning_details=reply.reasoning_details,
            parsed=parsed,
        )
