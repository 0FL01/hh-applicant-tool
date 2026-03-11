from hh_llm_agent.openrouter import OpenRouterChatClient


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
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        completions = FakeCompletions(responses)
        self.chat = type(
            "Chat",
            (),
            {"completions": completions},
        )()


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

    result = chat.complete_json([{"role": "user", "content": "hi"}])

    assert result.parsed["action"] == "reply"
    assert client.chat.completions.calls[0]["extra_body"] == {
        "reasoning": {"enabled": True}
    }


def test_complete_json_repairs_invalid_json_and_preserves_reasoning():
    client = FakeClient(
        [
            FakeResponse(
                "reply: sure",
                reasoning_details=[{"type": "reasoning.text", "text": "draft"}],
            ),
            FakeResponse(
                '{"action":"skip","reply_text":"","reason":"nothing_to_add"}'
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

    result = chat.complete_json([{"role": "user", "content": "hi"}])

    assert result.parsed["action"] == "skip"
    repair_messages = client.chat.completions.calls[1]["messages"]
    assert repair_messages[-2]["reasoning_details"] == [
        {"type": "reasoning.text", "text": "draft"}
    ]
