"""回复草稿生成：provider 回退链，失败抛 DraftError 由工作流降级为 notify。"""

from __future__ import annotations

import re
from typing import Callable

from app.agent.prompts import build_draft_messages
from app.agent.router import RouteContext
from app.llm.provider import ProviderCallError, ProviderConfig, sdk_text_completion


class DraftError(RuntimeError):
    pass


CompletionFn = Callable[[ProviderConfig, list[dict[str, str]], int], str]


def _clean_draft(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:\w+)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


class Drafter:
    def __init__(
        self,
        providers: list[ProviderConfig],
        *,
        timeout: int = 60,
        completion_fn: CompletionFn | None = None,
    ):
        self._providers = providers
        self._timeout = timeout
        self._completion = completion_fn or sdk_text_completion

    def generate(
        self,
        context: RouteContext,
        *,
        feedback: str | None = None,
        previous_draft: str | None = None,
    ) -> str:
        messages = build_draft_messages(
            context, feedback=feedback, previous_draft=previous_draft
        )
        errors: list[str] = []
        for provider in self._providers:
            try:
                raw = self._completion(provider, messages, self._timeout)
                text = _clean_draft(raw)
                if text:
                    return text
                errors.append(f"{provider.name} 返回空草稿")
            except ProviderCallError as exc:
                errors.append(str(exc))
        raise DraftError("；".join(errors) or "未配置可用的 LLM provider")
