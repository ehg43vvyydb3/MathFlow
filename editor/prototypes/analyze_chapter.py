"""한 단원(연속 페이지 범위) 전체에 segment+VLM 파이프라인을 돌린다.

"1. 평면좌표" 단원(10~33쪽, 목차 기준)을 대상으로, 21쪽에서 검증한 로직이
이 책 전체에 반복되는 다른 템플릿(필수예제/연습문제/특강/단원 도입 등)에서도
쓸만하게 통하는지 확인한다. 페이지별 blocks.json 조각과 시각화를 저장하고,
전체 통계(타입 분포, needs_review 비율)를 낸다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "editor"))

import cv2

from mathflow_editor.analysis import pipeline, segment
from mathflow_editor.analysis.vlm_client import OllamaBackend

PDF_PATH = Path.home() / "Downloads" / "공통수학2.pdf"
PAGE_RANGE = range(10, 34)  # 목차 기준 "1. 평면좌표" 단원, 10~33쪽 (inclusive)
BOOK_ID = "gongtong-math-2"

PROJECT_DIR = REPO_ROOT / "editor" / "projects" / BOOK_ID
OUT_DIR = Path(
    "/private/tmp/claude-501/-Users-giljisu-Developer-MathFlow/"
    "de04aef4-1aa7-4903-bfb7-ede6eb8d6b27/scratchpad/chapter1"
)

_TYPE_COLOR = {
    "text": (255, 150, 0),
    "figure": (0, 200, 0),
    "formula": (0, 0, 255),
    "table": (200, 0, 200),
    "problem_number": (0, 200, 200),
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache = pipeline.BlockCache(PROJECT_DIR / "vlm_cache.json")
    backend = OllamaBackend()

    all_blocks = []
    total_counts: dict[str, int] = {}
    total_needs_review = 0

    for page_number in PAGE_RANGE:
        page_index = page_number - 1
        entries = pipeline.run_page(
            PDF_PATH, page_index=page_index, page_number=page_number, dpi=150, backend=backend, cache=cache
        )
        cache.save()

        img = segment.render_page(PDF_PATH, page_index, 150)
        h, w = img.shape[:2]
        page_counts: dict[str, int] = {}
        page_needs_review = 0
        for e in entries:
            b = e["block"]
            all_blocks.append(b)
            page_counts[b["type"]] = page_counts.get(b["type"], 0) + 1
            total_counts[b["type"]] = total_counts.get(b["type"], 0) + 1
            if e["needs_review"]:
                page_needs_review += 1
                total_needs_review += 1

            x, y, bw, bh = b["bbox"]
            x0, y0, x1, y1 = int(x * w), int(y * h), int((x + bw) * w), int((y + bh) * h)
            color = _TYPE_COLOR.get(b["type"], (128, 128, 128))
            thickness = 1 if e["needs_review"] else 2
            cv2.rectangle(img, (x0, y0), (x1, y1), color, thickness)
            cv2.putText(img, b["type"][:4], (x0, max(0, y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

        cv2.imwrite(str(OUT_DIR / f"page{page_number}.png"), img)
        print(f"page {page_number:3d}: {len(entries):2d}개 블록, {page_counts}, needs_review={page_needs_review}")

    (OUT_DIR / "all_blocks.json").write_text(
        json.dumps(all_blocks, ensure_ascii=False, indent=2)
    )

    print(f"\n=== 단원 전체 ({len(list(PAGE_RANGE))}페이지, 블록 {len(all_blocks)}개) ===")
    print(f"타입 분포: {total_counts}")
    print(f"needs_review: {total_needs_review}/{len(all_blocks)} ({total_needs_review/len(all_blocks)*100:.1f}%)")


if __name__ == "__main__":
    main()
