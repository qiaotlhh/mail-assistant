"""草稿生成 SDK 调用模式验收：文本草稿不能强制 JSON object。"""

from __future__ import annotations

from app.agent.drafter import Drafter
from app.agent.prompts import build_draft_messages
from app.agent.router import RouteContext
from app.llm.provider import ProviderConfig


class FakeResponse:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return FakeResponse(self.content)


class FakeClient:
    def __init__(self, content):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(content)


def test_default_drafter_completion_does_not_force_json_mode(monkeypatch):
    client = FakeClient("您好，这是文本草稿。")
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    drafter = Drafter(
        [ProviderConfig("deepseek", "key", "deepseek-chat", "https://example.com")]
    )

    draft = drafter.generate(
        RouteContext(
            sender="alice@example.com",
            subject="地点确认",
            body="你知道那个地点在哪吗？",
            headers={},
        )
    )

    assert draft == "您好，这是文本草稿。"
    assert len(client.chat.completions.requests) == 1
    assert "response_format" not in client.chat.completions.requests[0]


def test_draft_prompt_uses_default_signature_tql():
    messages = build_draft_messages(
        RouteContext(
            sender="alice@example.com",
            subject="地点确认",
            body="你知道那个地点在哪吗？",
            headers={},
        )
    )
    assert "默认署名固定为“tql”" in messages[0]["content"]


def test_rewrite_prompt_treats_feedback_as_confirmed_fact():
    messages = build_draft_messages(
        RouteContext(
            sender="alice@example.com",
            subject="地点确认",
            body="你知道那个地点在哪吗？",
            headers={},
        ),
        feedback="地点为昌平区",
        previous_draft="上一版草稿",
    )

    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]
    assert "人工审核反馈是邮箱主人已确认的事实" in system_prompt
    assert "不要把反馈提供的信息再转成向发件人的追问" in system_prompt
    assert "人工审核反馈（必须落实）：\n地点为昌平区" in user_prompt