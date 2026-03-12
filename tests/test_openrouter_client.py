import sys
import types

import pytest

openai_module = types.ModuleType("openai")
openai_module.OpenAI = object
sys.modules.setdefault("openai", openai_module)

from hh_llm_agent.openrouter import (
    OpenRouterChatClient,
    OpenRouterError,
    StructuredOutputSchema,
)


TEST_SCHEMA = StructuredOutputSchema(
    name="reply_decision",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "reply_text": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action", "reply_text", "reason"],
        "additionalProperties": False,
    },
)


class FakeMessage:
    def __init__(self, content, reasoning_details=None):
        self.content = content
        self.reasoning_details = reasoning_details


class FakeResponse:
    def __init__(self, content, reasoning_details=None):
        message = FakeMessage(content, reasoning_details)
        self.choices = [type("Choice", (), {"message": message})()]


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        completions = FakeCompletions(responses)
        self.chat = type(
            "Chat",
            (),
            {"completions": completions},
        )()


class FakeOpenRouterError(Exception):
    def __init__(self, message, *, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_complete_json_parses_first_try():
    client = FakeClient(
        [
            FakeResponse(
                '{"action":"reply","reply_text":"Здравствуйте!","reason":"need_reply"}'
            )
        ]
    )
    chat = OpenRouterChatClient(
        config=type(
            "Cfg",
            (),
            {
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "token",
                "referer": "https://example.com",
                "app_name": "test",
                "model": "google/gemini-3.1-flash-lite-preview",
                "temperature": 0.2,
                "max_completion_tokens": 100,
                "reasoning_enabled": True,
            },
        )(),
        client=client,
    )

    result = chat.complete_json(
        [{"role": "user", "content": "hi"}],
        schema=TEST_SCHEMA,
    )

    assert result.parsed["action"] == "reply"
    assert client.chat.completions.calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "reply_decision",
            "strict": True,
            "schema": TEST_SCHEMA.schema,
        },
    }
    assert client.chat.completions.calls[0]["extra_body"] == {
        "reasoning": {"enabled": True},
        "provider": {"require_parameters": True},
        "plugins": [{"id": "response-healing"}],
    }


def test_complete_json_raises_for_invalid_structured_json():
    client = FakeClient(
        [
            FakeResponse(
                "reply: sure",
                reasoning_details=[{"type": "reasoning.text", "text": "draft"}],
            ),
        ]
    )
    chat = OpenRouterChatClient(
        config=type(
            "Cfg",
            (),
            {
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "token",
                "referer": "https://example.com",
                "app_name": "test",
                "model": "google/gemini-3.1-flash-lite-preview",
                "temperature": 0.2,
                "max_completion_tokens": 100,
                "reasoning_enabled": True,
            },
        )(),
        client=client,
    )

    with pytest.raises(OpenRouterError, match="invalid structured JSON"):
        chat.complete_json(
            [{"role": "user", "content": "hi"}],
            schema=TEST_SCHEMA,
        )

    assert len(client.chat.completions.calls) == 1


def test_complete_json_retries_without_require_parameters_on_provider_404():
    client = FakeClient(
        [
            FakeOpenRouterError(
                "Error code: 404",
                status_code=404,
                body={
                    "error": {
                        "message": "No endpoints found that can handle the requested parameters."
                    }
                },
            ),
            FakeResponse(
                '{"action":"reply","reply_text":"Здравствуйте!","reason":"need_reply"}'
            ),
        ]
    )
    chat = OpenRouterChatClient(
        config=type(
            "Cfg",
            (),
            {
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "token",
                "referer": "https://example.com",
                "app_name": "test",
                "model": "google/gemma-3-27b-it",
                "temperature": 0.0,
                "max_completion_tokens": 100,
                "reasoning_enabled": False,
            },
        )(),
        client=client,
    )

    result = chat.complete_json(
        [{"role": "user", "content": "hi"}],
        schema=TEST_SCHEMA,
    )

    assert result.parsed["action"] == "reply"
    assert len(client.chat.completions.calls) == 2
    assert client.chat.completions.calls[0]["extra_body"] == {
        "reasoning": {"enabled": False},
        "provider": {"require_parameters": True},
        "plugins": [{"id": "response-healing"}],
    }
    assert client.chat.completions.calls[1]["extra_body"] == {
        "reasoning": {"enabled": False},
        "plugins": [{"id": "response-healing"}],
    }
