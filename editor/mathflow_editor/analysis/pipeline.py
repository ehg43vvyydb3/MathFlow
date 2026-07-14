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

# 뷰어의 리플로우 컨테이너는 폰 화면 폭에 맞춰지고(좌우 패딩 14px씩 제외),
# 페이지는 150dpi로 렌더링돼 폭이 ~1073px 안팎이다(실측 12쪽 기준). 흔한
# 안드로이드 기준 폭인 360px(컨테이너 360-28=332px)보다 좁은 폰에서, 정규화
# 폭이 이 비율(332/1073)보다 넓은 한 줄짜리 블록은 원본 해상도보다 작게
# 줄어들어(scale<1) 글자가 부자연스럽게 작아진다 — 처음에 380px 기준으로
# 잡았더니 정작 이 기능의 계기였던 12쪽 필수 12번 문제(정규화 폭 0.3541)가
# 근소한 차이(0.35415)로 걸러지지 않아 360px로 낮췄다. 이미 여러 줄로
# 쪼개진 블록(len(line_boxes) > 1)은 각 줄이 이미 짧으니 대상에서 제외한다.
WRAP_WIDTH_THRESHOLD = 332 / 1073


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


def _apply_type_rules(vlm_type: str, bbox_norm: list[float], img: np.ndarray, box: segment.Box) -> str:
    """VLM 분류 결과에 기하학적/색상 상식 규칙을 덧씌운다.

    10~12쪽 실편집에서 VLM이 소단원 배지("1 수직선 위의...")처럼 폭이 넓은
    블록을 problem_number로 자주 오분류하는 패턴이 확인됐다. 진짜 문제번호는
    페이지 폭의 ~3% 수준이므로, 폭이 넓으면 text로 교정한다.

    반대 방향 오류도 있다: "연습문제" 절의 자주색 문제번호(segment.
    split_colored_leading_label이 간격과 무관하게 분리해내는 블록)를 VLM이
    text로 잘못 읽는 경우가 완료 10~64쪽 diff에서 66건으로 가장 큰 타입변경
    카테고리였다 — 색 자체가 이미 problem_number라는 강한 신호이므로 VLM
    답과 무관하게 덮어쓴다.
    """
    _x, _y, w, _h = bbox_norm
    if vlm_type == "problem_number" and w > 0.08:
        return "text"
    if vlm_type != "problem_number" and segment.is_practice_number_color(img, box):
        return "problem_number"
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
    # "정답 및 풀이 영상" QR코드는 콘텐츠가 아니라 완전히 제외한다 — 150dpi로는
    # 위치조차 못 찾아서 900dpi로 따로 다시 렌더링해 찾는다(segment.detect_qr_codes
    # 참고). 못 지우면 옆 문제번호/본문 블록의 x범위를 왼쪽으로 넓혀서 자기
    # 블록 없이 이웃 블록에 흡수돼 버린다(148쪽 실측).
    qr_boxes = segment.detect_qr_codes(pdf_path, page_index, w, h)
    boxes = segment.detect_blocks(img, qr_boxes=qr_boxes)
    mask_lines = segment.compute_mask_lines(img)  # text 블록의 줄 단위 분리용, 페이지당 한 번
    mask_words = segment.compute_mask_words(img)  # 긴 한 줄 강제 줄바꿈의 단어 간격 검출용, 페이지당 한 번
    if qr_boxes:
        segment.blank_boxes(mask_lines, qr_boxes)
        segment.blank_boxes(mask_words, qr_boxes)

    # "필수 05"/"확인체크 12" 같은 원형 색상 배지는 detect_blocks가 못 잡는다
    # (PLAN.md, 23페이지 diff에서 가장 큰 미해결 패턴) — 색으로 따로 찾아 채워
    # 넣는다. 아직 완벽하지 않은 검출(발전 배지 등 다른 색 계열은 못 잡음,
    # 드물게 위치가 살짝 어긋남)이라 VLM 분류를 거치지 않고 problem_number로
    # 바로 넣되 needs_review=True로 표시해서 사람이 검토 UI에서 반드시
    # 한 번 확인/보정하게 한다 — 자동으로 그냥 믿지 않는다.
    def _iou(a: segment.Box, b: segment.Box) -> float:
        ix0, iy0 = max(a.x0, b.x0), max(a.y0, b.y0)
        ix1, iy1 = min(a.x1, b.x1), min(a.y1, b.y1)
        if ix1 <= ix0 or iy1 <= iy0:
            return 0.0
        inter = (ix1 - ix0) * (iy1 - iy0)
        area_a = (a.x1 - a.x0) * (a.y1 - a.y0)
        area_b = (b.x1 - b.x0) * (b.y1 - b.y0)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    badge_boxes = [
        bb for bb in segment.detect_icon_badges(img) if not any(_iou(bb, ob) > 0.2 for ob in boxes)
    ]

    prefix = id_prefix or f"p{page_number}"
    total = len(boxes) + len(badge_boxes)

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

        block_type = _apply_type_rules(cached["type"], box.norm(w, h), img, box)
        block = {
            "id": f"{prefix}_b{i:02d}",
            "page": page_number,
            "type": block_type,
            "bbox": box.norm(w, h),
            "order": i,
            "confidence": cached["confidence"],
            "reflow": {"role": ROLE_BY_TYPE.get(block_type, "paragraph")},
        }
        if block_type in ("text", "formula"):
            # figure/table은 줄 사이 2차원적 위치 관계 자체가 의미라 쪼개면 안
            # 된다. formula는 분수·지수처럼 위험해 보이지만, 실제로는 분수
            # 막대(또는 그 일부)가 해당 행에 잉크를 남기기 때문에 "행 전체가
            # 거의 빈" 지점에서만 끊는 이 로직이 분수를 관통해서 자르는 일이
            # 없음을 실제 블록(p24_bm14, p18_b08 등, 분수+설명문 혼합)으로
            # 확인했다 — 여러 줄 유도 과정(=... =... ∴...)이 흔히 섞여 있어서
            # 그 부분만이라도 줄 단위로 쌓으면 좁은 폭에서 더 크게 보여줄 수 있다.
            line_boxes = segment.detect_lines_in_box(mask_lines, box)
            if len(line_boxes) > 1:
                # 문단이 이미 여러 줄로 나뉘었어도, 그 줄 하나하나가 여전히 화면
                # 폭 기준으로 너무 넓을 수 있다(15쪽 p15_b09, 3줄 중 앞 2줄이 폭
                # 0.657·0.656 — 문단 전체가 아니라 개별 줄 단위로도 같은 검사를
                # 해야 그 줄들이 마저 잘린다). 각 줄을 따로 확인해서 필요하면
                # 그 줄만 추가로 2등분한다.
                final_lines: list[segment.Box] = []
                for lb in line_boxes:
                    if lb.norm(w, h)[2] > WRAP_WIDTH_THRESHOLD:
                        wrapped_line = segment.wrap_long_line(mask_words, lb)
                        if wrapped_line is not None:
                            final_lines.extend(wrapped_line)
                            continue
                    final_lines.append(lb)
                block["lines"] = [{"bbox": fb.norm(w, h)} for fb in final_lines]
            elif box.norm(w, h)[2] > WRAP_WIDTH_THRESHOLD:
                # 이미 여러 줄로 안 쪼개졌는데(=한 줄) 화면 폭 기준으로 너무 넓은
                # 경우 — 자연스러운 단어 간격에서 억지로 2등분해서 각 반쪽이 더
                # 크게 보이게 한다(12쪽 필수 12번 "두 점에서 ~ 점 Q의" 같은 사례).
                wrapped = segment.wrap_long_line(mask_words, box)
                if wrapped is not None:
                    block["lines"] = [{"bbox": wb.norm(w, h)} for wb in wrapped]
        # blocks.schema.json은 additionalProperties: false라 needs_review를
        # 블록 안에 못 넣는다 — 별도 래퍼로 감싸서 스키마 오염 없이 전달.
        results.append({"block": block, "needs_review": bool(cached.get("needs_review"))})

        if on_progress is not None:
            on_progress(i + 1, total)

    # 색상으로 찾은 배지는 VLM을 거치지 않고 바로 problem_number로 넣는다 —
    # 이미 타입을 색으로 확신하는데 VLM에 다시 물어볼 이유가 없다. 대신
    # needs_review=True로 항상 표시(위 설명 참고).
    for j, box in enumerate(badge_boxes):
        if should_stop is not None and should_stop():
            break
        i = len(boxes) + j
        block = {
            "id": f"{prefix}_b{i:02d}",
            "page": page_number,
            "type": "problem_number",
            "bbox": box.norm(w, h),
            "order": i,
            "confidence": 0.6,
            "reflow": {"role": ROLE_BY_TYPE["problem_number"]},
        }
        results.append({"block": block, "needs_review": True})
        if on_progress is not None:
            on_progress(i + 1, total)
    return results
