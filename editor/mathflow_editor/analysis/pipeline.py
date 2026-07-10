"""segment(위치) + vlm_client(타입)를 묶어 한 페이지의 blocks.json 항목을 만든다.

같은 블록 이미지는 다시 분류하지 않도록 이미지 해시 기준으로 캐싱한다
(재실행해도 API 재과금/재추론이 없게).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from . import segment
from .vlm_client import VLMBackend

ROLE_BY_TYPE = {
    "text": "paragraph",
    "figure": "figure",
    "formula": "equation",
    "table": "table",
    "problem_number": "label",
}


class BlockCache:
    """블록 이미지 해시 -> VLM 분류 결과. 책 단위로 하나씩 둔다."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict = json.loads(path.read_text()) if path.exists() else {}

    def get(self, image_hash: str) -> dict | None:
        return self._data.get(image_hash)

    def set(self, image_hash: str, entry: dict) -> None:
        self._data[image_hash] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))


def crop_png(img: np.ndarray, box: segment.Box) -> bytes:
    crop = img[box.y0 : box.y1, box.x0 : box.x1]
    ok, buf = cv2.imencode(".png", crop)
    return buf.tobytes()


def _apply_type_rules(vlm_type: str, bbox_norm: list[float]) -> str:
    """VLM 분류 결과에 기하학적 상식 규칙을 덧씌운다.

    10~12쪽 실편집에서 VLM이 소단원 배지("1 수직선 위의...")처럼 폭이 넓은
    블록을 problem_number로 자주 오분류하는 패턴이 확인됐다. 진짜 문제번호는
    페이지 폭의 ~3% 수준이므로, 폭이 넓으면 text로 교정한다.
    """
    _x, _y, w, _h = bbox_norm
    if vlm_type == "problem_number" and w > 0.08:
        return "text"
    return vlm_type


def run_page(
    pdf_path: Path,
    page_index: int,
    page_number: int,
    dpi: int,
    backend: VLMBackend,
    cache: BlockCache,
    id_prefix: str | None = None,
    force: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict]:
    """PDF의 한 페이지를 세그멘테이션 + VLM 분류해서 blocks.json 항목 리스트로 낸다.

    force=True면 캐시에 있어도 무시하고 VLM을 다시 호출한다 (프롬프트/모델을
    바꾼 뒤 기존 캐시가 낡았을 때 "새로 캐싱" 메뉴에서 사용).

    on_progress(done, total)는 블록 하나 처리할 때마다 불린다. VLM 호출이
    블록당 몇 초씩 걸려서, 페이지 단위로만 진행 상황을 알리면 UI가 한참
    멈춘 것처럼 보인다 — 호출자가 이 콜백에서 진행창을 갱신하고
    processEvents를 돌려 화면이 계속 반응하게 만드는 용도.
    should_stop()이 True를 반환하면 남은 블록은 처리하지 않고 그때까지
    분류한 것만 반환한다 (취소 버튼이 페이지 중간에도 바로 먹히게).
    """
    img = segment.render_page(pdf_path, page_index, dpi)
    h, w = img.shape[:2]
    boxes = segment.detect_blocks(img)
    prefix = id_prefix or f"p{page_number}"
    total = len(boxes)

    results = []
    for i, box in enumerate(boxes):
        if should_stop is not None and should_stop():
            break

        png_bytes = crop_png(img, box)
        image_hash = hashlib.sha256(png_bytes).hexdigest()

        cached = None if force else cache.get(image_hash)
        if cached is None:
            result = backend.classify(png_bytes)
            cached = {"type": result.type, "confidence": result.confidence, "needs_review": result.needs_review}
            cache.set(image_hash, cached)

        block_type = _apply_type_rules(cached["type"], box.norm(w, h))
        block = {
            "id": f"{prefix}_b{i:02d}",
            "page": page_number,
            "type": block_type,
            "bbox": box.norm(w, h),
            "order": i,
            "confidence": cached["confidence"],
            "reflow": {"role": ROLE_BY_TYPE.get(block_type, "paragraph")},
        }
        # blocks.schema.json은 additionalProperties: false라 needs_review를
        # 블록 안에 못 넣는다 — 별도 래퍼로 감싸서 스키마 오염 없이 전달.
        results.append({"block": block, "needs_review": bool(cached.get("needs_review"))})

        if on_progress is not None:
            on_progress(i + 1, total)
    return results
