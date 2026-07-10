"""검토 UI가 쓰는 "재검토 필요" 휴리스틱.

VLM 자체 확신도는 거의 항상 ~0.95로 나와서 신호가 안 된다는 게 실측으로
확인됐다 (needs_review 0/422, 24페이지 실측). 대신 실제로 관찰된 지배적
오류 패턴 — "번호/수식이 본문과 병합되어 블록이 비정상적으로 커지는 것" —
을 기하학적으로 잡는다: 같은 페이지, 같은 타입의 블록들 중 면적이 유난히
크면 병합 실패 가능성이 높다고 보고 플래그한다.
"""
from __future__ import annotations


def flag_needs_review(blocks: list[dict], area_ratio_thresh: float = 2.5) -> list[bool]:
    """블록 리스트(같은 페이지)에 대해 병합-실패 의심 블록에 True를 매긴다."""
    areas_by_type: dict[str, list[float]] = {}
    for b in blocks:
        x, y, w, h = b["bbox"]
        areas_by_type.setdefault(b["type"], []).append(w * h)

    medians: dict[str, float] = {}
    for t, areas in areas_by_type.items():
        sorted_areas = sorted(areas)
        n = len(sorted_areas)
        medians[t] = sorted_areas[n // 2] if n % 2 else (sorted_areas[n // 2 - 1] + sorted_areas[n // 2]) / 2

    flags = []
    for b in blocks:
        x, y, w, h = b["bbox"]
        area = w * h
        median = max(medians.get(b["type"], area), 1e-6)
        flags.append(area > area_ratio_thresh * median)
    return flags
