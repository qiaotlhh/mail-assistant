"""LLM 适配层：provider 回退、结构化输出重试与错误分级。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.agent.prompts import build_router_messages
from app.agent.router import RouteContext, RouteResult
from app.config import Settings
from app.llm.schemas import RouteDecision, parse_route_decision
from app.store.models import EmailCategory, RouteSource


class LLMErrorKind(str, Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    OTHER = "other"


class ProviderCallError(RuntimeError):
    def __init__(self, kind: LLMErrorKind, provider: str, message: str):
        super().__init__(f"[{provider}] {message}")
        self.kind = kind
        self.provider = provider


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    model: str
    base_url: str | None


CompletionFn = Callable[[ProviderConfig, list[dict[str, str]], int], str]


def build_providers(settings: Settings) -> list[ProviderConfig]:
    """按 LLM_PROVIDER_ORDER 构建回退链；未配置 Key 的 provider 跳过。"""
    providers: list[ProviderConfig] = []
    for name in settings.provider_order:
        if name == "deepseek":
            api_key, base_url, model = (
                settings.deepseek_api_key,
                settings.deepseek_base_url,
                settings.deepseek_model,
            )
        else:
            api_key, base_url, model = (
                settings.openai_api_key,
                settings.openai_base_url,
                settings.openai_model,
            )
        if api_key:
            providers.append(
                ProviderConfig(name=name, api_key=api_key, model=model, base_url=base_url)
            )
    return providers


class LLMRouter:
    """结构化分类：同 provider 解析重试，错误后回退下一 provider，最终兜底 notify。"""

    PARSE_MAX_ATTEMPTS = 3

    def __init__(
        self,
        providers: list[ProviderConfig],
        *,
        min_confidence: float = 0.7,
        timeout: int = 60,
        completion_fn: CompletionFn | None = None,
    ):
        if not 0.0 < min_confidence <= 1.0:
            raise ValueError("min_confidence 必须在 (0, 1] 区间")
        self._providers = providers
        self._min_confidence = min_confidence
        self._timeout = timeout
        self._completion = completion_fn or sdk_completion
        self.last_alerts: list[str] = []

    def route(self, context: RouteContext) -> RouteResult:
        self.last_alerts = []
        if not self._providers:
            self.last_alerts.append("未配置任何可用的 LLM provider")
            return self._fallback("未配置任何可用的 LLM provider")
        for provider in self._providers:
            result = self._route_with_provider(provider, context)
            if result is not None:
                return result
        return self._fallback("；".join(self.last_alerts))

    def _route_with_provider(
        self, provider: ProviderConfig, context: RouteContext
    ) -> RouteResult | None:
        base_messages = build_router_messages(context)
        feedback_messages: list[dict[str, str]] = []
        last_error = ""
        for attempt in range(1, self.PARSE_MAX_ATTEMPTS + 1):
            try:
                raw = self._completion(
                    provider, base_messages + feedback_messages, self._timeout
                )
                decision = parse_route_decision(raw)
            except ProviderCallError as exc:
                self._record_call_error(exc)
                return None
            except ValueError as exc:
                last_error = str(exc) or exc.__class__.__name__
                if attempt < self.PARSE_MAX_ATTEMPTS:
                    feedback_messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"上一次输出无法解析为合法结构：{last_error[:200]}。"
                                "请严格只输出一个 json 对象，不要包含 markdown 代码块或任何多余文字。"
                            ),
                        }
                    )
                    continue
                self.last_alerts.append(
                    f"{provider.name} 结构化输出解析失败"
                    f"（已重试 {self.PARSE_MAX_ATTEMPTS - 1} 次）：{last_error[:200]}"
                )
                return None
            return self._to_result(decision)
        return None

    def _to_result(self, decision: RouteDecision) -> RouteResult:
        if decision.confidence < self._min_confidence:
            return RouteResult(
                category=EmailCategory.NOTIFY,
                reason=(
                    f"LLM 置信度 {decision.confidence:.2f} 低于阈值 "
                    f"{self._min_confidence}，降级为待通知"
                    f"（原判定 {decision.category}：{decision.reason}）"
                ),
                source=RouteSource.LLM,
                confidence=decision.confidence,
            )
        return RouteResult(
            category=EmailCategory(decision.category),
            reason=decision.reason,
            source=RouteSource.LLM,
            confidence=decision.confidence,
        )

    def _record_call_error(self, exc: ProviderCallError) -> None:
        if exc.kind is LLMErrorKind.AUTH:
            self.last_alerts.append(
                f"LLM 配置告警：{exc.provider} 鉴权失败，请检查 API Key"
            )
        elif exc.kind is LLMErrorKind.TIMEOUT:
            self.last_alerts.append(f"{exc.provider} 请求超时，回退下一个 provider")
        elif exc.kind is LLMErrorKind.RATE_LIMIT:
            self.last_alerts.append(f"{exc.provider} 触发限流，回退下一个 provider")
        else:
            self.last_alerts.append(f"{exc.provider} 调用失败，回退下一个 provider：{exc}")

    @staticmethod
    def _fallback(reason: str) -> RouteResult:
        return RouteResult(
            category=EmailCategory.NOTIFY,
            reason=f"LLM 路由失败，默认进入待通知：{reason}",
            source=RouteSource.FALLBACK,
            confidence=0.0,
        )


def sdk_completion(
    provider: ProviderConfig, messages: list[dict[str, str]], timeout: int
) -> str:
    """结构化分类调用：强制 JSON object 输出。"""
    return _sdk_completion(provider, messages, timeout, json_mode=True)


def sdk_text_completion(
    provider: ProviderConfig, messages: list[dict[str, str]], timeout: int
) -> str:
    """普通文本调用：草稿生成不使用 JSON object 模式。"""
    return _sdk_completion(provider, messages, timeout, json_mode=False)


def _sdk_completion(
    provider: ProviderConfig,
    messages: list[dict[str, str]],
    timeout: int,
    *,
    json_mode: bool,
) -> str:
    """真实调用：openai SDK 兼容 DeepSeek / OpenAI，异常按等级包装。"""
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        OpenAI,
        PermissionDeniedError,
        RateLimitError,
    )

    client = OpenAI(
        api_key=provider.api_key, base_url=provider.base_url, timeout=timeout
    )
    try:
        request = {
            "model": provider.model,
            "messages": messages,
            "temperature": 0,
        }
        if json_mode:
            request["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**request)
        return response.choices[0].message.content or ""
    except AuthenticationError as exc:
        raise ProviderCallError(LLMErrorKind.AUTH, provider.name, f"鉴权失败：{exc}") from exc
    except PermissionDeniedError as exc:
        raise ProviderCallError(LLMErrorKind.AUTH, provider.name, f"无权限（403）：{exc}") from exc
    except RateLimitError as exc:
        raise ProviderCallError(LLMErrorKind.RATE_LIMIT, provider.name, f"限流：{exc}") from exc
    except (APITimeoutError, APIConnectionError) as exc:
        raise ProviderCallError(LLMErrorKind.TIMEOUT, provider.name, f"超时/连接失败：{exc}") from exc
    except APIStatusError as exc:
        raise ProviderCallError(LLMErrorKind.OTHER, provider.name, f"HTTP {exc.status_code}：{exc}") from exc


def build_llm_router(
    settings: Settings, completion_fn: CompletionFn | None = None
) -> LLMRouter:
    return LLMRouter(
        build_providers(settings),
        min_confidence=settings.router_llm_min_confidence,
        timeout=settings.llm_timeout_seconds,
        completion_fn=completion_fn,
    )
