"""OCR 없이 이미지 분석만으로 페이지에서 블록 위치(bbox)를 뽑는다 (RLSA 계열).

1. 페이지를 이진화해서 잉크(글자/선) 마스크를 만든다.
2. 세로 방향 잉크 밀도로 컬럼(본문/사이드바)을 나눈다.
3. 각 컬럼 안에서 가로 투영(row projection)으로 "줄" 단위 밴드를 뽑는다.
4. 인접한 줄 사이의 세로 간격이 작으면 같은 블록으로 합친다 (문단 vs 다음 블록 구분).

여기서는 위치(bbox)만 낸다. 타입 분류는 vlm_client가 담당한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import fitz
import numpy as np


@dataclass
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    def norm(self, w: int, h: int) -> list[float]:
        return [
            round(self.x0 / w, 4),
            round(self.y0 / h, 4),
            round((self.x1 - self.x0) / w, 4),
            round((self.y1 - self.y0) / h, 4),
        ]


def render_page(pdf_path: Path, page_index: int, dpi: int) -> np.ndarray:
    """PDF의 한 페이지를 BGR numpy 이미지로 렌더링한다."""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    return img


def ink_mask(gray: np.ndarray, min_component_area: int = 4) -> np.ndarray:
    """어두운 픽셀(글자/선/음영)을 255로 하는 이진 마스크.

    스캔 노이즈(먼지, 압축 아티팩트)가 컬럼/줄 분리를 방해하지 않도록
    아주 작은 연결 성분은 제거한다.
    """
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    clean = np.zeros_like(mask)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_component_area:
            clean[labels == i] = 255
    return clean


def detect_columns(
    mask: np.ndarray, min_gap_px: int = 15, density_thresh_ratio: float = 0.004
) -> list[tuple[int, int]]:
    """세로 방향 잉크 밀도가 낮은 구간(거터)을 찾아 컬럼으로 분할.

    완전히 0인 컬럼은 실제 스캔 페이지에 거의 없으므로(먼지, 옅은 배경
    텍스처 등) 페이지 높이에 비례한 작은 임계값 이하를 "거터"로 본다.
    """
    col_density = mask.sum(axis=0) / 255
    h = mask.shape[0]
    is_empty = col_density < max(2, h * density_thresh_ratio)
    w = mask.shape[1]

    cols: list[tuple[int, int]] = []
    x = 0
    while x < w:
        if is_empty[x]:
            x += 1
            continue
        start = x
        while x < w and not is_empty[x]:
            x += 1
        cols.append((start, x))
    # 인접 컬럼 사이 간격이 min_gap_px보다 작으면 병합 (글자 내부 흰틈 오검출 방지)
    merged: list[tuple[int, int]] = []
    for c in cols:
        if merged and c[0] - merged[-1][1] < min_gap_px:
            merged[-1] = (merged[-1][0], c[1])
        else:
            merged.append(c)
    return merged


def detect_line_bands(
    mask: np.ndarray, x0: int, x1: int, density_thresh_px: int = 2
) -> list[tuple[int, int]]:
    """컬럼 내부에서 가로 투영으로 줄 단위 y 밴드를 뽑는다."""
    col = mask[:, x0:x1]
    row_density = col.sum(axis=1) / 255
    is_ink = row_density > density_thresh_px

    bands: list[tuple[int, int]] = []
    y = 0
    h = mask.shape[0]
    while y < h:
        if not is_ink[y]:
            y += 1
            continue
        start = y
        while y < h and is_ink[y]:
            y += 1
        bands.append((start, y))
    return bands


def group_bands_to_blocks(
    bands: list[tuple[int, int]], gap_thresh_px: int
) -> list[tuple[int, int]]:
    """줄 밴드 사이 간격이 gap_thresh_px 이하면 같은 블록으로 합친다."""
    if not bands:
        return []
    blocks = [bands[0]]
    for y0, y1 in bands[1:]:
        prev_y0, prev_y1 = blocks[-1]
        if y0 - prev_y1 <= gap_thresh_px:
            blocks[-1] = (prev_y0, y1)
        else:
            blocks.append((y0, y1))
    return blocks


def detect_blocks(img: np.ndarray) -> list[Box]:
    """페이지 이미지에서 블록 후보 bbox 리스트를 뽑는다 (타입 분류 없음)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = ink_mask(gray)

    # 글자 내부 획 사이 틈을 메워 "줄"로 뭉치기 (가로 방향 closing)
    line_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3))
    mask_lines = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, line_kernel)

    columns = detect_columns(mask_lines, min_gap_px=20)

    # 줄 높이 중앙값으로 "같은 문단 vs 다음 블록" 판단 간격을 정한다.
    line_heights = []
    for x0, x1 in columns:
        bands = detect_line_bands(mask_lines, x0, x1)
        for y0, y1 in bands:
            line_heights.append(y1 - y0)
    median_h = float(np.median(line_heights)) if line_heights else 20.0
    gap_thresh = max(6, int(median_h * 0.9))

    boxes: list[Box] = []
    for x0, x1 in columns:
        bands = detect_line_bands(mask_lines, x0, x1)
        # 너무 작은(잡음) 밴드 제거
        bands = [b for b in bands if (b[1] - b[0]) >= 3]
        blocks = group_bands_to_blocks(bands, gap_thresh)
        for y0, y1 in blocks:
            # 블록 내부에서 실제 잉크가 있는 x 범위로 폭을 다시 타이트하게
            sub = mask_lines[y0:y1, x0:x1]
            col_density = sub.sum(axis=0)
            xs = np.nonzero(col_density)[0]
            if len(xs) == 0:
                continue
            bx0, bx1 = x0 + xs.min(), x0 + xs.max() + 1
            box = Box(int(bx0), int(y0), int(bx1), int(y1))
            if _is_debris(box):
                continue
            boxes.append(box)

    boxes = _split_leading_labels(mask_lines, boxes)
    boxes = _split_tall_lines(mask_lines, boxes)
    boxes = [b for b in boxes if not _is_debris(b)]
    return [_pad_box(b, mask.shape[1], mask.shape[0]) for b in boxes]


def _is_debris(box: Box, min_side_px: int = 7, min_area_px: int = 400) -> bool:
    """장식 괘선 조각, 스캔 먼지 같은 미세 블록인지 판정.

    10~12쪽 실편집에서 사용자가 일관되게 삭제한 블록들(예: 4~8px 두께의
    장식 괘선 조각, 10px 미만 점 조각)에서 도출한 기준. 정상 문제번호도
    150dpi에서 ~23x30px라 이 기준에 안 걸린다. 분할 단계에서 생기는
    조각도 있어 분할 후에도 한 번 더 거른다.
    """
    w, h = box.x1 - box.x0, box.y1 - box.y0
    return w < min_side_px or h < min_side_px or w * h < min_area_px


def _pad_box(box: Box, page_w: int, page_h: int, pad: int = 3) -> Box:
    """박스에 여유 패딩을 준다.

    루트 기호 지붕, 선분 기호(AB 위 가로줄)처럼 얇은 획은 이진화/노이즈 제거
    단계에서 끊겨 타이트한 bbox 밖으로 밀려나기 쉽다. 몇 px의 여유가 잘림을
    크게 줄이고, 리플로우에서 크롭이 살짝 커지는 부작용은 무시할 수준이다.
    """
    return Box(
        max(0, box.x0 - pad),
        max(0, box.y0 - pad),
        min(page_w, box.x1 + pad),
        min(page_h, box.y1 + pad),
    )


def _internal_line_bands(mask_lines: np.ndarray, box: Box) -> list[tuple[int, int]]:
    """블록 내부의 줄 밴드들을 (블록 기준 상대 y0,y1)로 반환."""
    sub = mask_lines[box.y0 : box.y1, box.x0 : box.x1]
    row_density = sub.sum(axis=1) / 255
    is_ink = row_density > 2
    bands: list[tuple[int, int]] = []
    y = 0
    while y < sub.shape[0]:
        if not is_ink[y]:
            y += 1
            continue
        start = y
        while y < sub.shape[0] and is_ink[y]:
            y += 1
        bands.append((start, y))
    return bands


def _retighten_x(mask_lines: np.ndarray, box: Box) -> Box:
    """블록 y범위 안에서 실제 잉크가 있는 x 범위로 폭을 다시 타이트하게."""
    sub = mask_lines[box.y0 : box.y1, box.x0 : box.x1]
    xs = np.nonzero(sub.sum(axis=0))[0]
    if len(xs) == 0:
        return box
    return Box(box.x0 + int(xs.min()), box.y0, box.x0 + int(xs.max()) + 1, box.y1)


def _first_line_components(
    mask_lines: np.ndarray, box: Box
) -> tuple[int, int, list[tuple[int, int, int]]] | None:
    """블록의 첫 줄 (y0,y1)과 그 줄의 연결 성분(x0,width,height) 리스트를 반환."""
    bands = _internal_line_bands(mask_lines, box)
    if not bands:
        return None
    line_y0, line_y1 = bands[0]

    first_line = mask_lines[box.y0 + line_y0 : box.y0 + line_y1, box.x0 : box.x1]
    n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(first_line, connectivity=8)
    comps = [
        (
            int(stats[i, cv2.CC_STAT_LEFT]),
            int(stats[i, cv2.CC_STAT_WIDTH]),
            int(stats[i, cv2.CC_STAT_HEIGHT]),
        )
        for i in range(1, n)
    ]
    comps.sort()
    return line_y0, line_y1, comps


def split_leading_label(
    mask_lines: np.ndarray,
    box: Box,
    gap_ratio_thresh: float = 2.2,
    min_gap_px: int = 15,
) -> tuple[Box | None, Box]:
    """블록 첫 줄에서 유난히 넓은 간격 뒤에 떨어진 선행 라벨(문제번호 등)을 분리한다.

    "26  다음 그림..."처럼 번호 뒤 간격이 그 줄의 일반 단어 사이 간격보다
    뚜렷이 넓으면 번호만 별도 박스로 뗀다. "(1) 점 D는..."처럼 정상적인
    단어 간격이면 그대로 둔다 (문제집에서 최상위 문제번호는 굵고 큰 폰트에
    뒤에 여백을 둬서 조판하지만, 항목 번호 "(1)"은 본문과 같은 간격으로
    흘러가는 조판 관례를 이용한 구분이다).
    """
    parsed = _first_line_components(mask_lines, box)
    if parsed is None:
        return None, box
    line_y0, line_y1, comps = parsed
    if len(comps) < 3:
        return None, box

    gaps = [comps[i + 1][0] - (comps[i][0] + comps[i][1]) for i in range(len(comps) - 1)]
    leading_gap = gaps[0]
    rest_gaps = gaps[1:]
    if not rest_gaps:
        return None, box
    median_rest_gap = float(np.median(rest_gaps))

    if leading_gap < min_gap_px or leading_gap < gap_ratio_thresh * max(median_rest_gap, 1.0):
        return None, box

    label_x1 = box.x0 + comps[0][0] + comps[0][1]
    rest_x0 = box.x0 + comps[1][0]

    label_box = Box(box.x0, box.y0 + line_y0, label_x1, box.y0 + line_y1)
    rest_box = Box(rest_x0, box.y0, box.x1, box.y1)
    return label_box, rest_box


def _split_leading_labels(mask_lines: np.ndarray, boxes: list[Box]) -> list[Box]:
    result: list[Box] = []
    for box in boxes:
        label_box, rest_box = split_leading_label(mask_lines, box)
        if label_box is not None:
            result.append(label_box)
        result.append(rest_box)
    return result


def split_tall_line(
    mask_lines: np.ndarray,
    box: Box,
    height_ratio_thresh: float = 1.35,
) -> tuple[Box, Box | None]:
    """블록 내부에서 유난히 키가 큰 줄(분수 등 다단 수식)이 나오면 그 앞에서 자른다.

    "설명문 + 박스 수식"처럼 세로로 이어붙은 서로 다른 성격의 블록은 줄 간격만으로는
    잘 안 갈린다(간격이 문단 내 줄간격보다 조금 큰 정도라 애매함). 대신 수식은
    분수/첨자 때문에 줄 높이 자체가 본문보다 확연히 크다는 신호를 쓴다. 앞쪽 "정상"
    줄들의 중앙값보다 height_ratio_thresh배 넘게 큰 줄이 나오면 거기서부터 새 블록.
    """
    bands = _internal_line_bands(mask_lines, box)
    if len(bands) < 2:
        return box, None
    heights = [y1 - y0 for y0, y1 in bands]

    for i in range(1, len(bands)):
        prefix_median = float(np.median(heights[:i]))
        if heights[i] > height_ratio_thresh * max(prefix_median, 1.0):
            split_y = bands[i][0]
            top = _retighten_x(mask_lines, Box(box.x0, box.y0, box.x1, box.y0 + split_y))
            bottom = _retighten_x(mask_lines, Box(box.x0, box.y0 + split_y, box.x1, box.y1))
            return top, bottom
    return box, None


def _split_tall_lines(mask_lines: np.ndarray, boxes: list[Box]) -> list[Box]:
    result: list[Box] = []
    for box in boxes:
        top, bottom = split_tall_line(mask_lines, box)
        result.append(top)
        if bottom is not None:
            result.append(bottom)
    return result


def draw_boxes(
    img: np.ndarray, boxes: list[Box], color: tuple[int, int, int] = (0, 0, 255)
) -> np.ndarray:
    out = img.copy()
    for b in boxes:
        cv2.rectangle(out, (b.x0, b.y0), (b.x1, b.y1), color, 2)
    return out
