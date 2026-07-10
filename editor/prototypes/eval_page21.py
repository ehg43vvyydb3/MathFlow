"""pipeline.py(segment+VLM)을 21페이지에 돌려서 손라벨 정답과 비교한다.

`shared/schema/examples/blocks.json`(15블록, 타입 포함)을 정답으로 삼아
위치(IoU)뿐 아니라 타입 정확도까지 확인한다.
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
GROUND_TRUTH = REPO_ROOT / "shared" / "schema" / "examples" / "blocks.json"
OUT_DIR = Path(
    "/private/tmp/claude-501/-Users-giljisu-Developer-MathFlow/"
    "de04aef4-1aa7-4903-bfb7-ede6eb8d6b27/scratchpad"
)


def iou(a: list[float], b: list[float]) -> float:
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


_TYPE_COLOR = {
    "text": (255, 150, 0),
    "figure": (0, 200, 0),
    "formula": (0, 0, 255),
    "table": (200, 0, 200),
    "problem_number": (0, 200, 200),
}


def main() -> None:
    cache = pipeline.BlockCache(REPO_ROOT / "editor" / "projects" / "gongtong-math-2" / "vlm_cache.json")
    backend = OllamaBackend()

    entries = pipeline.run_page(PDF_PATH, page_index=20, page_number=21, dpi=150, backend=backend, cache=cache)
    cache.save()

    gt = json.loads(GROUND_TRUTH.read_text())
    gt_blocks = [b for b in gt["blocks"] if b["page"] == 21]

    print(f"=== 검출 {len(entries)}개 vs 정답 {len(gt_blocks)}개 ===")
    hits_pos = 0
    hits_type = 0
    for gb in gt_blocks:
        best = max(entries, key=lambda e: iou(gb["bbox"], e["block"]["bbox"]))
        best_iou = iou(gb["bbox"], best["block"]["bbox"])
        pos_ok = best_iou >= 0.3
        type_ok = pos_ok and best["block"]["type"] == gb["type"]
        hits_pos += pos_ok
        hits_type += type_ok
        mark = "OK  " if type_ok else ("TYPE" if pos_ok else "MISS")
        print(
            f"  [{mark}] {gb['id']:12s} 정답={gb['type']:15s} "
            f"예측={best['block']['type']:15s} iou={best_iou:.2f} "
            f"needs_review={best['needs_review']}"
        )
    print(f"\n위치 커버리지: {hits_pos}/{len(gt_blocks)} (IoU>=0.3)")
    print(f"타입 정확도(위치 맞은 것 중): {hits_type}/{hits_pos if hits_pos else 1}")
    print(f"needs_review 개수: {sum(e['needs_review'] for e in entries)}/{len(entries)}")

    img = segment.render_page(PDF_PATH, 20, 150)
    for e in entries:
        b = e["block"]
        h, w = img.shape[:2]
        x, y, bw, bh = b["bbox"]
        x0, y0, x1, y1 = int(x * w), int(y * h), int((x + bw) * w), int((y + bh) * h)
        color = _TYPE_COLOR.get(b["type"], (128, 128, 128))
        thickness = 1 if e["needs_review"] else 2
        cv2.rectangle(img, (x0, y0), (x1, y1), color, thickness)
        cv2.putText(img, b["type"][:4], (x0, max(0, y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

    out_path = OUT_DIR / "page21_classified.png"
    cv2.imwrite(str(out_path), img)
    print(f"\n시각화 저장: {out_path}")


if __name__ == "__main__":
    main()
