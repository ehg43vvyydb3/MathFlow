"""완료 페이지의 저장본을 "지금 코드"의 자동분석 결과와 비교해 편집 패턴을 뽑는다.

단원 하나를 다 끝냈을 때 이걸 돌려서 리포트를 보고, 반복되는 패턴이 있으면
segment.py(위치)/vlm_client.py(프롬프트)/pipeline.py(사후 규칙) 중 어디에
반영할지 사람이 판단한다 — 판단 자체는 자동화하지 않는다 (표본이 적어
과적합 위험이 크고, 어떤 코드를 어떻게 고칠지는 여전히 사람 몫).

블록 ID 문자열이 아니라 IoU(겹침)로 매칭한다. 세그멘테이션 로직이 여러 번
바뀌다 보면 "p12_b13" 같은 ID가 전혀 다른 블록을 가리키게 될 수 있어서,
ID로 매칭하면 가짜 타입변경/리사이즈 신호가 잔뜩 잡힌다 (실제로 겪은 문제).

사용법:
    python diff_edits.py                       # status.json의 완료 페이지 전부
    python diff_edits.py --pages 10-24          # 특정 범위만
    python diff_edits.py --force                # 캐시 무시하고 VLM 재호출
                                                  # (프롬프트를 막 바꿔서 캐시가
                                                  # 낡았을 때만 쓰기 — 느림)

리포트는 stdout에만 찍고 파일로 따로 저장하지 않는다 — 나중에 다시 보려면
직접 리다이렉트할 것:
    python diff_edits.py --pages 10-32 > report.txt
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "editor"))

from mathflow_editor.analysis import pipeline  # noqa: E402
from mathflow_editor.analysis.vlm_client import OllamaBackend  # noqa: E402
from mathflow_editor.io import metadata  # noqa: E402

PDF_PATH = Path.home() / "Downloads" / "공통수학2.pdf"
BOOK_ID = "gongtong-math-2"
OUTPUT_DIR = REPO_ROOT / "editor" / "output" / BOOK_ID
PROJECT_DIR = REPO_ROOT / "editor" / "projects" / BOOK_ID

IOU_MATCH_THRESHOLD = 0.5
RESIZE_THRESHOLD = 0.01  # bbox 값(정규화 좌표) 차이가 이보다 크면 "크기 조정"으로 봄


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


def match_blocks(
    auto_blocks: list[dict], edited_blocks: list[dict]
) -> tuple[list[tuple[dict, dict, float]], list[dict], list[dict]]:
    """IoU로 매칭. 반환: (매칭쌍, 자동에만 있음=진짜삭제, 저장본에만 있음=진짜추가)."""
    used_edited_ids: set[str] = set()
    matched: list[tuple[dict, dict, float]] = []
    for a in auto_blocks:
        best, best_iou = None, 0.0
        for e in edited_blocks:
            if e["id"] in used_edited_ids:
                continue
            i = iou(a["bbox"], e["bbox"])
            if i > best_iou:
                best, best_iou = e, i
        if best is not None and best_iou >= IOU_MATCH_THRESHOLD:
            matched.append((a, best, best_iou))
            used_edited_ids.add(best["id"])

    matched_auto_ids = {a["id"] for a, _, _ in matched}
    deleted = [a for a in auto_blocks if a["id"] not in matched_auto_ids]
    added = [e for e in edited_blocks if e["id"] not in used_edited_ids]
    return matched, deleted, added


def parse_page_range(spec: str) -> list[int]:
    start, end = spec.split("-")
    return list(range(int(start), int(end) + 1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=str, default=None, help="예: 10-24 (생략하면 완료 페이지 전부)")
    ap.add_argument("--force", action="store_true", help="캐시 무시하고 VLM 재호출 (느림)")
    args = ap.parse_args()

    status = metadata.load_status(OUTPUT_DIR / "status.json")
    if args.pages:
        pages = parse_page_range(args.pages)
    else:
        pages = sorted(p for p, s in status.items() if s == "done")

    if not pages:
        print("대상 페이지가 없습니다 (완료 표시된 페이지가 없거나 --pages 지정 필요).")
        return

    edited_all = metadata.load_blocks(OUTPUT_DIR / "blocks.json")["blocks"]
    cache = pipeline.BlockCache(PROJECT_DIR / "vlm_cache.json")
    backend = OllamaBackend()

    deleted_by_key: dict[tuple[str, str], list[tuple[int, dict]]] = defaultdict(list)
    added_by_key: dict[tuple[str, str], list[tuple[int, dict]]] = defaultdict(list)
    type_changed: list[tuple[int, dict, dict]] = []
    resized: list[tuple[int, dict, dict]] = []

    def region(bbox: list[float]) -> str:
        return "사이드바" if bbox[0] > 0.65 else "본문"

    for page in pages:
        entries = pipeline.run_page(
            PDF_PATH,
            page_index=page - 1,
            page_number=page,
            dpi=150,
            backend=backend,
            cache=cache,
            force=args.force,
        )
        cache.save()
        auto_blocks = [e["block"] for e in entries]
        edited_blocks = [b for b in edited_all if b["page"] == page]

        matched, deleted, added = match_blocks(auto_blocks, edited_blocks)

        for b in deleted:
            deleted_by_key[(b["type"], region(b["bbox"]))].append((page, b))
        for b in added:
            added_by_key[(b["type"], region(b["bbox"]))].append((page, b))
        for a, e, _ in matched:
            if a["type"] != e["type"]:
                type_changed.append((page, a, e))
            elif any(abs(x - y) > RESIZE_THRESHOLD for x, y in zip(a["bbox"], e["bbox"])):
                resized.append((page, a, e))

    print(f"=== 대상: {len(pages)}페이지 ({pages[0]}~{pages[-1]}) ===\n")

    print("### 삭제된 블록 (자동분석엔 있는데 저장본엔 없음 — 과다 검출/조각남)")
    for (btype, reg), items in sorted(deleted_by_key.items(), key=lambda kv: -len(kv[1])):
        pages_str = ", ".join(str(p) for p, _ in items[:6]) + (" ..." if len(items) > 6 else "")
        print(f"  [{len(items):2d}건] {btype:15s} {reg:5s}  페이지: {pages_str}")

    print("\n### 추가된 블록 (저장본엔 있는데 자동분석엔 없음 — 과소 검출, 수동 병합/재작성)")
    for (btype, reg), items in sorted(added_by_key.items(), key=lambda kv: -len(kv[1])):
        pages_str = ", ".join(str(p) for p, _ in items[:6]) + (" ..." if len(items) > 6 else "")
        print(f"  [{len(items):2d}건] {btype:15s} {reg:5s}  페이지: {pages_str}")

    print(f"\n### 타입 변경 ({len(type_changed)}건, IoU 매칭이라 신뢰도 높음)")
    type_pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    for _page, a, e in type_changed:
        type_pair_counts[(a["type"], e["type"])] += 1
    for (o, n), count in sorted(type_pair_counts.items(), key=lambda kv: -kv[1]):
        print(f"  [{count:2d}건] {o} -> {n}")

    print(f"\n### 크기만 조정 ({len(resized)}건) — 대체로 개별 미세조정이라 패턴화하기 어려움")


if __name__ == "__main__":
    main()
