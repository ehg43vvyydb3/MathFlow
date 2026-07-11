"""블록 크롭 이미지를 비전 모델(VLM)에 던져 타입을 분류한다.

세그멘테이션(segment.py)은 위치만 뽑으므로, "이 블록이 text/figure/formula/
table/problem_number 중 무엇인가"는 여기서 결정한다. 백엔드는 로컬 Ollama와
OpenRouter(저렴한 비전 모델) 두 가지를 같은 인터페이스로 지원한다 —
오프라인이 필요하면 Ollama, 속도/품질이 더 필요하면 OpenRouter로 바꿀 수 있게.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Protocol

import requests

BLOCK_TYPES = ["text", "figure", "formula", "table", "problem_number"]

PROMPT = f"""이 이미지는 스캔한 수학 교과서 페이지에서 잘라낸 블록 하나다.
다음 중 하나로 분류하라: {", ".join(BLOCK_TYPES)}
- text: 일반 문단/문장. 수식이 한두 개 섞여 있어도, "~하면", "따라서", "즉"처럼
  문장이 이어지며 풀이를 설명하는 내용이면 text다 (formula로 분류하지 말 것).
- figure: 그림, 도형, 그래프
- formula: 설명 문장 없이 수식 자체가 여러 줄에 걸쳐 나열된 것
  (예: "AB²=AC²+BC² \n =|x2-x1|²+|y2-y1|² \n ∴ AB=√(...)")
- table: 표
- problem_number: 문제 번호만 있는 작은 라벨 (예: "26", "(1)")

다른 설명 없이 아래 JSON 형식으로만 답하라:
{{"type": "<위 카테고리 중 하나>", "confidence": <0.0~1.0 사이 숫자>}}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class ClassificationResult:
    type: str
    confidence: float
    needs_review: bool
    raw: str


def _parse_response(text: str) -> ClassificationResult:
    match = _JSON_RE.search(text)
    if not match:
        return ClassificationResult(type="text", confidence=0.0, needs_review=True, raw=text)
    try:
        data = json.loads(match.group(0))
        block_type = data["type"]
        confidence = float(data["confidence"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return ClassificationResult(type="text", confidence=0.0, needs_review=True, raw=text)

    if block_type not in BLOCK_TYPES:
        return ClassificationResult(type="text", confidence=0.0, needs_review=True, raw=text)

    return ClassificationResult(
        type=block_type,
        confidence=confidence,
        needs_review=confidence < 0.6,
        raw=text,
    )


class VLMBackend(Protocol):
    def classify(self, image_bytes: bytes) -> ClassificationResult: ...


class OllamaBackend:
    """로컬 Ollama 백엔드. API 키 불필요, 오프라인 동작."""

    def __init__(self, model: str = "qwen2.5vl:7b", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def classify(self, image_bytes: bytes) -> ClassificationResult:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        resp = requests.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": PROMPT,
                "images": [b64],
                "stream": False,
            },
            timeout=300,  # 모델 콜드 로드/메모리 압박 시 60초로는 부족한 사례가 실제로 발생
        )
        resp.raise_for_status()
        text = resp.json()["response"]
        return _parse_response(text)


class OpenRouterBackend:
    """OpenRouter 백엔드. OPENROUTER_API_KEY 필요."""

    def __init__(self, api_key: str, model: str = "qwen/qwen2.5-vl-3b-instruct:free"):
        self.api_key = api_key
        self.model = model

    def classify(self, image_bytes: bytes) -> ClassificationResult:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            },
                        ],
                    }
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return _parse_response(text)
