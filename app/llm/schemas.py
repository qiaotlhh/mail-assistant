"""LLM 结构化输出 schema 与宽容解析。"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RouteDecision(BaseModel):
    category: Literal["ignore", "notify", "respond"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        return value.strip()


def parse_route_decision(raw: str) -> RouteDecision:
    """解析模型输出：剥掉 markdown 代码块、容忍前后缀说明文字。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    return RouteDecision.model_validate(data)
