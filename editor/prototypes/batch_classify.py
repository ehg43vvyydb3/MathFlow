"""여러 페이지에 segment+VLM 파이프라인을 돌려 시각화로 스팟체크한다.

21페이지 전용 정답이 없는 새 페이지들이라 IoU 정확도 계산은 못 하고,
타입별 색상 오버레이 이미지를 저장해서 눈으로 확인하는 용도.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "editor"))

import cv2

from mathflow_editor.analysis import pipeline, segment
from mathflow_editor.analysis.vlm_client import OllamaBackend

PDF_PATH = Path.home() / "Downloads" / "공통수학2.pdf"
OUT_DIR = Path(
    "/private/tmp/claude-501/-Users-giljisu-Developer-MathFlow/"
    "de04aef4-1aa7-4903-bfb7-ede6eb8d6b27/scratchpad"
)

_TYPE_COLOR = {
    "text": (255, 150, 0),
    "figure": (0, 200, 0),
    "formula": (0, 0, 255),
    "table": (200, 0, 200),
    "problem_number": (0, 200, 200),
}

# 21쪽과 같은 "개념원리 익히기" 템플릿
SAME_TEMPLATE_PAGES = [14, 22, 23, 30]
# 참고용: 다른 템플릿("필수" 예제 스타일)
OTHER_TEMPLATE_PAGES = [26]


def classify_and_render(page_number: int, backend, cache) -> list[dict]:
    page_index = page_number - 1
    entries = pipeline.run_page(
        PDF_PATH, page_index=page_index, page_number=page_number, dpi=150, backend=backend, cache=cache
    )
    img = segment.render_page(PDF_PATH, page_index, 150)
    for e in entries:
        b = e["block"]
        h, w = img.shape[:2]
        x, y, bw, bh = b["bbox"]
        x0, y0, x1, y1 = int(x * w), int(y * h), int((x + bw) * w), int((y + bh) * h)
        color = _TYPE_COLOR.get(b["type"], (128, 128, 128))
        thickness = 1 if e["needs_review"] else 2
        cv2.rectangle(img, (x0, y0), (x1, y1), color, thickness)
        cv2.putText(img, b["type"][:4], (x0, max(0, y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
    out_path = OUT_DIR / f"page{page_number}_classified.png"
    cv2.imwrite(str(out_path), img)
    return entries


def main() -> None:
    cache = pipeline.BlockCache(REPO_ROOT / "editor" / "projects" / "gongtong-math-2" / "vlm_cache.json")
    backend = OllamaBackend()

    for page_number in SAME_TEMPLATE_PAGES + OTHER_TEMPLATE_PAGES:
        entries = classify_and_render(page_number, backend, cache)
        cache.save()
        counts: dict[str, int] = {}
        for e in entries:
            counts[e["block"]["type"]] = counts.get(e["block"]["type"], 0) + 1
        needs_review = sum(e["needs_review"] for e in entries)
        print(f"page {page_number:3d}: {len(entries):2d}개 블록, {counts}, needs_review={needs_review}")


if __name__ == "__main__":
    main()
