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


def compute_mask_lines(img: np.ndarray) -> np.ndarray:
    """글자 내부 획 사이 틈을 메운 잉크 마스크 (줄 단위로 뭉쳐짐).

    detect_blocks 내부에서도 쓰지만, 블록 타입이 확정된 뒤(text만) 줄 단위로
    다시 쪼갤 때(detect_lines_in_box)도 필요해서 재사용 가능하게 분리했다.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = ink_mask(gray)
    line_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, line_kernel)


def detect_lines_in_box(mask_lines: np.ndarray, box: Box) -> list[Box]:
    """블록 내부의 개별 줄을 절대 좌표 Box 리스트로 뽑는다.

    text 타입에만 쓴다 — formula/figure/table은 줄 사이 2차원적 위치 관계
    자체가 의미라서(분수선, 지수 등) 줄 단위로 쪼개면 안 된다.
    """
    lines: list[Box] = []
    for ry0, ry1 in _internal_line_bands(mask_lines, box):
        if (ry1 - ry0) < 3:  # 1~2px짜리는 닫힘 연산 잔여물 등 잡음이지 줄이 아니다
            continue
        sub = mask_lines[box.y0 + ry0 : box.y0 + ry1, box.x0 : box.x1]
        xs = np.nonzero(sub.sum(axis=0))[0]
        if len(xs) == 0:
            continue
        lx0, lx1 = box.x0 + int(xs.min()), box.x0 + int(xs.max()) + 1
        lines.append(Box(lx0, box.y0 + ry0, lx1, box.y0 + ry1))
    return lines


def compute_mask_words(img: np.ndarray) -> np.ndarray:
    """글자 획 사이 틈만 메운(음절 내부는 붙되 음절/단어 사이 간격은 남기는) 마스크.

    compute_mask_lines의 (9,3) 커널은 줄 전체를 한 덩어리로 뭉치는 게 목적이라
    닫는 힘이 너무 세다 — 실측(p12_b14, "x축 위의 점의 좌표는 (a, 0), y축
    위의 점의 좌표는 (0,b)로 놓는다.")에서 그 커널을 단어 간격 찾기에 그대로
    쓰면 연결성분이 2개(gap 1개)로만 뭉개져서 억지 분할 지점이 그 하나뿐이었고,
    하필 "(a," 와 "0)" 사이라는 어색한 자리에서 끊겼다. (5,3)은 같은 이미지에서
    연결성분 14개(gap 13개)를 남겨 실제 어절 경계("y축"/"위의" 사이)가 중앙에
    가장 가까운 후보로 자연스럽게 뽑혔다.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = ink_mask(gray)
    word_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, word_kernel)


def _line_components(mask_words: np.ndarray, box: Box) -> list[tuple[int, int, int]]:
    """박스(이미 한 줄로 간주) 내부의 연결 성분을 (x0, width, height)로, x순 정렬.

    폭 3px 미만인 성분은 제외한다 — 15쪽 p15_b07에서 표/그림 경계선이 박스
    가장자리에 살짝 걸려 들어온 폭 1px짜리 성분이 하나 섞여 있었는데, 이걸
    실제 내용으로 치면 "중간 지점"이 진짜 글자 범위를 한참 벗어난 위치로
    계산돼 분할했을 때 한쪽이 거의 텅 비는 문제가 있었다.
    """
    sub = mask_words[box.y0 : box.y1, box.x0 : box.x1]
    n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(sub, connectivity=8)
    comps = [
        (int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT]))
        for i in range(1, n)
        if stats[i, cv2.CC_STAT_WIDTH] >= 3
    ]
    comps.sort()
    return comps


def wrap_long_line(mask_words: np.ndarray, box: Box, min_gap_px: int = 4) -> list[Box] | None:
    """자연적인 줄바꿈이 없는 긴 한 줄을, 중간 지점에 가장 가까운 자연스러운
    단어 간격에서 억지로 2등분한다.

    뷰어가 화면 폭에 맞추려고 원본보다 작게 줄여야 하는(스케일 < 1) 긴 문장을
    이걸로 미리 쪼개 두면, 각 반쪽은 폭이 절반이라 그만큼 배율을 더 키울 수
    있다 — 진짜 텍스트 줄바꿈과 같은 효과. 적당한 간격을 못 찾으면 None.
    """
    comps = _line_components(mask_words, box)
    if len(comps) < 2:
        return None

    content_x0 = comps[0][0]
    content_x1 = comps[-1][0] + comps[-1][1]
    box_w = box.x1 - box.x0
    if box_w > 0 and (content_x1 - content_x0) / box_w < 0.5:
        # 실제 잉크는 박스 폭의 절반도 안 채우는 경우 — 23페이지 전 범위 실측에서
        # 이런 블록은 "긴 문장"이 아니라 소제목 옆 장식용 가로줄이나 표/그림
        # 경계선 조각이 박스 가장자리에 살짝 끼어들어 박스 자체가 내용보다 훨씬
        # 넓게 잡힌 세그멘테이션 문제였다(15쪽 p15_b07 비율 0.25, 22쪽 "선분의
        # 내분점" 제목 줄 등 — 나머지 정상 줄은 전부 0.85 이상). 이런 박스를
        # 억지로 반으로 쪼개면 한쪽에 실제 내용이 거의 안 남는다 — 대신 실제
        # 잉크 범위로 타이트하게 좁힌 박스 하나만 돌려준다. 안 그러면 이 장식
        # 여백을 포함한 폭이 뷰어의 "가장 넓은 줄" 배율 계산에 끼어들어, 정작
        # 제대로 쪼갠 다른 줄들까지 안 커지는 문제로 이어진다(22쪽에서 확인).
        return [Box(box.x0 + content_x0, box.y0, box.x0 + content_x1, box.y1)]
    target_x = (content_x0 + content_x1) / 2

    best_i, best_dist = None, None
    for i in range(len(comps) - 1):
        gap_start = comps[i][0] + comps[i][1]
        gap_end = comps[i + 1][0]
        gap = gap_end - gap_start
        if gap < min_gap_px:
            continue
        gap_center = (gap_start + gap_end) / 2
        dist = abs(gap_center - target_x)
        if best_dist is None or dist < best_dist:
            best_dist, best_i = dist, i
    if best_i is None:
        return None

    gap_start = comps[best_i][0] + comps[best_i][1]
    gap_end = comps[best_i + 1][0]
    split_x = box.x0 + (gap_start + gap_end) // 2
    return [
        Box(box.x0, box.y0, split_x, box.y1),
        Box(split_x, box.y0, box.x1, box.y1),
    ]


# "필수"/"확인체크" 원형 배지(청록~파랑 계열) 색상 범위. 실측(11쪽 "필수 01":
# HSV H≈98, 22쪽 "확인체크 29": HSV H≈111, 둘 다 S 80~140·V 95~200 안)해서
# 두 라벨이 같은 브랜드 색 계열임을 확인했다 — 하나의 범위로 같이 잡는다.
BADGE_HUE_MIN = 85
BADGE_HUE_MAX = 125
BADGE_SAT_MIN = 60
BADGE_VAL_MIN = 40
BADGE_VAL_MAX = 250
BADGE_MIN_AREA_PX = 1000  # 안티앨리어싱 경계, 잡음, 소단원 스텝 번호(~650),
# "개념원리 이해" 배너의 스우시 왼쪽 조각(닫힘 커널로도 안정적으로 안 합쳐짐,
# 10·18쪽 실측 830~915)까지 제외 — 실제 배지 최소 조각은 1156 이상이라 여유 있음.
BADGE_MAX_AREA_PX = 4000
# 배지 옆 번호("29" 등)가 검정이 아니라 배지와 비슷한 짙은 남색 계열이라 색
# 마스크에 따로 걸리는 경우가 있다(22쪽 실측) — 번호 성분은 높이가 22px인 데
# 반해 실제 배지 원은 34~63px라 뚜렷이 낮으니, 높이로 번호 성분을 걸러낸다.
BADGE_MIN_HEIGHT_PX = 30
BADGE_MAX_HEIGHT_PX = 70
BADGE_MAX_WIDTH_PX = 90
# 이 브랜드 색(청록~파랑)이 문제 배지 말고도 다른 UI 장식에 재사용돼서 실측중
# 오탐 3종을 발견했다: (1) "개념원리 이해" 큰 섹션 헤더 장식 — 면적 ~5400~5600으로
# BADGE_MAX_AREA_PX보다 훨씬 큼. (2) 우측 상단 코너 스우시 장식(챕터 배지와
# 비슷한 자리, x0/page_w>0.80·y0/page_h<0.20) — 아래 코너 제외로 거른다.
# (3) 소단원 안 스텝 번호("1 수직선 우" 등) — 면적 ~620~680으로 BADGE_MIN_AREA_PX
# 미만. (1)(3)은 크기로, (2)는 위치로 제외한다.
BADGE_CORNER_X_MIN = 0.80
BADGE_CORNER_Y_MAX = 0.20


def detect_icon_badges(img: np.ndarray) -> list[Box]:
    """"필수 05"/"확인체크 12"처럼 번호가 원형 색상 배지에 붙어 나오는
    problem_number를 색상으로 찾는다.

    이 배지는 원 안에 라벨 글자("필수"/"확인체크")까지 있고 번호와의 간격이
    일정치 않아서, 투영 기반 세그멘테이션(detect_blocks)이 아예 못 뽑고
    놓친다 — 23페이지 diff에서 이 패턴이 28건으로 가장 큰 미해결 패턴이었다
    (PLAN.md 참고). 원(아이콘)을 색으로 먼저 찾고, 그 오른쪽에 붙은 검정
    숫자까지 bbox를 넓혀서 problem_number 하나로 반환한다.
    """
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h_ch, s_ch, v_ch = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    color_mask = (
        (h_ch >= BADGE_HUE_MIN)
        & (h_ch <= BADGE_HUE_MAX)
        & (s_ch >= BADGE_SAT_MIN)
        & (v_ch >= BADGE_VAL_MIN)
        & (v_ch <= BADGE_VAL_MAX)
    ).astype(np.uint8) * 255
    # 원 안에 흰 글자("확인"/"체크" 2줄 등)가 있으면 색 성분이 글자 획을 따라
    # 여러 조각으로 쪼개진다(실측: 29쪽 "확인체크 44"가 2개 성분으로 분리돼
    # IoU 0.19까지 떨어짐) — 닫힘 연산으로 그 틈을 메워 하나의 원으로 합친다.
    # 17px로 키우면 "필수" 배지(원+텍스트가 폭 넓게 갈라짐)는 합쳐지고, 10쪽
    # "개념원리 이해" 섹션 배너의 스우시 조각과 "01" 숫자 부분도 합쳐져서
    # (143x111, 면적 7264) 아래 BADGE_MAX_AREA_PX로 걸러진다 — 반대로 배지
    # 원과 그 옆 번호("29" 등)는 22px 정도 떨어져 있어 17px로는 안 합쳐짐을
    # 확인했다(실측).
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17))
    color_mask = cv2.morphologyEx(color_mask, cv2.MORPH_CLOSE, close_kernel)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ink = ink_mask(gray)
    page_h, page_w = img.shape[:2]

    n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(color_mask, connectivity=8)
    boxes: list[Box] = []
    for i in range(1, n):
        x, y, w, hh = stats[i, 0], stats[i, 1], stats[i, 2], stats[i, 3]
        area = stats[i, cv2.CC_STAT_AREA]
        if not (BADGE_MIN_AREA_PX <= area <= BADGE_MAX_AREA_PX):
            continue
        if not (BADGE_MIN_HEIGHT_PX <= hh <= BADGE_MAX_HEIGHT_PX) or w > BADGE_MAX_WIDTH_PX:
            continue
        if x / page_w > BADGE_CORNER_X_MIN and y / page_h < BADGE_CORNER_Y_MAX:
            continue
        icon_box = Box(int(x), int(y), int(x + w), int(y + hh))
        boxes.append(_extend_with_adjacent_number(ink, icon_box, page_w))
    return boxes


def _extend_with_adjacent_number(
    ink: np.ndarray, icon_box: Box, page_w: int, max_gap_px: int = 15, edge_skip_px: int = 6
) -> Box:
    """원형 아이콘 오른쪽에 붙은 숫자(검정 글자)까지 bbox를 넓힌다.

    아이콘 원의 안티앨리어싱 테두리가 ink_mask에서 1px짜리 잉크로 잡혀 band의
    맨 앞(인덱스 0)에 걸린다(38~55쪽 실측 4건 전부 동일). 이걸 숫자의 시작으로
    착각하면 바로 뒤 실제 간격(원과 번호 사이 20~23px, 위 주석 "22px 정도"와
    일치)이 max_gap_px(15)보다 커서 진짜 번호에 닿기도 전에 확장을 멈춰버려
    bbox가 원 하나 너비(~48px)에 머문다("확인체크 68/71/104"의 실제 저장
    bbox는 100px 이상인데 자동분석은 48px에서 끊김). 원 테두리 잉크는 아이콘
    바로 옆 몇 px 안에서 끝나고 진짜 번호는 그보다 훨씬 뒤에서 시작하므로,
    맨 앞 edge_skip_px는 잉크가 있어도 "번호 시작"으로 치지 않는다.
    """
    x_limit = min(page_w, icon_box.x1 + 80)  # 두 자리 숫자까지 넉넉히 볼 폭
    band = ink[icon_box.y0 : icon_box.y1, icon_box.x1 : x_limit]
    if band.size == 0:
        return icon_box
    col_has_ink = band.sum(axis=0) > 0
    col_has_ink[:edge_skip_px] = False

    last_ink_x = None
    gap = 0
    for i, has_ink in enumerate(col_has_ink):
        if has_ink:
            last_ink_x = i
            gap = 0
        else:
            gap += 1
            if last_ink_x is not None and gap > max_gap_px:
                break
    if last_ink_x is None:
        return icon_box
    return Box(icon_box.x0, icon_box.y0, icon_box.x1 + last_ink_x + 1, icon_box.y1)


def detect_blocks(img: np.ndarray) -> list[Box]:
    """페이지 이미지에서 블록 후보 bbox 리스트를 뽑는다 (타입 분류 없음)."""
    mask_lines = compute_mask_lines(img)

    columns = detect_columns(mask_lines, min_gap_px=20)

    # 줄 높이 중앙값으로 "같은 문단 vs 다음 블록" 판단 간격을 정한다.
    line_heights = []
    for x0, x1 in columns:
        bands = detect_line_bands(mask_lines, x0, x1)
        for y0, y1 in bands:
            line_heights.append(y1 - y0)
    median_h = float(np.median(line_heights)) if line_heights else 20.0
    gap_thresh = max(6, int(median_h * 0.9))

    # 이 책은 본문(왼쪽)+사이드바(오른쪽 ~30%) 2단 구성이 반복된다. 사이드바의
    # 그림(좌표평면 등)은 내부에 여백이 커서, 본문 기준 gap_thresh로는 한 그림이
    # 여러 조각으로 쪼개진다 — 실제 편집 로그에서 반복적으로 관찰된 패턴
    # (10, 12, 15, 17, 18, 19, 20쪽에서 사용자가 조각난 그림을 수동으로 병합).
    # 사이드바 컬럼만 간격 허용치를 넉넉하게 잡는다.
    page_w = mask_lines.shape[1]
    page_h = mask_lines.shape[0]
    SIDEBAR_X_RATIO = 0.65
    SIDEBAR_GAP_MULTIPLIER = 2.2

    boxes: list[Box] = []
    for x0, x1 in columns:
        bands = detect_line_bands(mask_lines, x0, x1)
        # 너무 작은(잡음) 밴드 제거
        bands = [b for b in bands if (b[1] - b[0]) >= 3]
        col_gap_thresh = gap_thresh
        if x0 / page_w > SIDEBAR_X_RATIO:
            col_gap_thresh = int(gap_thresh * SIDEBAR_GAP_MULTIPLIER)
        blocks = group_bands_to_blocks(bands, col_gap_thresh)
        for y0, y1 in blocks:
            # 블록 내부에서 실제 잉크가 있는 x 범위로 폭을 다시 타이트하게
            sub = mask_lines[y0:y1, x0:x1]
            col_density = sub.sum(axis=0)
            xs = np.nonzero(col_density)[0]
            if len(xs) == 0:
                continue
            bx0, bx1 = x0 + xs.min(), x0 + xs.max() + 1
            box = Box(int(bx0), int(y0), int(bx1), int(y1))
            if _is_debris(box, page_w, page_h):
                continue
            boxes.append(box)

    boxes = _split_leading_labels(mask_lines, boxes)
    boxes = _split_tall_lines(mask_lines, boxes)
    boxes = [b for b in boxes if not _is_debris(b, page_w, page_h)]
    return [_pad_box(b, mask_lines.shape[1], mask_lines.shape[0]) for b in boxes]


# 우측 상단 챕터 배지(둥근 탭 "Ⅰ-1" + 그 아래 세로로 회전된 소단원명) 영역.
# 이 책 전체에서 페이지마다 거의 같은 자리(x0 0.91~0.92, y0 0.08 안팎)에
# 나오는 순수 내비게이션 장식이라 실제 학습 콘텐츠가 아니다 — 23페이지 diff에서
# 사용자가 6번 전부 일관되게 지운 걸 확인(13·19·21·23·25·31쪽), 크롭해서 실제로
# "Ⅰ-1"+세로 텍스트인 것도 눈으로 확인했다. "더 다양한 문제는 RPM..." 같은 다른
# 헤더 장식은 실제 문제 내용과 한 블록에 뭉쳐 있어서 통째로 지우면 위험해 제외했다.
#
# Y_MAX=0.20은 "평면좌표"(4자) 소단원명 기준이었는데, 28쪽 diff(10~37쪽)에서
# "직선의 방정식"(6자)인 35·37쪽은 이 문턱을 못 넘어 그대로 검출됐고 사람이
# 수작업으로 지웠다(문제_number/text로 각각 분류됨). 크롭해서 세로 배지+
# 위쪽 장식 스우시 꼬리까지 y1을 실측해보니 소단원명 길이에 따라 0.24~0.32까지
# 늘어난다(예: 35쪽 0.24, 45쪽 0.24, 105·115쪽 0.32) — 반면 x0>0.90인 자리에는
# 이 배지/스우시 말고 실제 콘텐츠가 나온 적이 없다(사람이 검토 완료한 10~37쪽
# 저장본 전수 확인). 그래서 문턱을 여유 있게 0.35로 올린다.
CORNER_BADGE_X_MIN = 0.90
CORNER_BADGE_Y_MAX = 0.35


def _is_debris(box: Box, page_w: int, page_h: int, min_side_px: int = 7, min_area_px: int = 400) -> bool:
    """장식 괘선 조각, 스캔 먼지, 챕터 배지 같은 비-콘텐츠 블록인지 판정.

    10~12쪽 실편집에서 사용자가 일관되게 삭제한 블록들(예: 4~8px 두께의
    장식 괘선 조각, 10px 미만 점 조각)에서 도출한 기준. 정상 문제번호도
    150dpi에서 ~23x30px라 이 기준에 안 걸린다. 분할 단계에서 생기는
    조각도 있어 분할 후에도 한 번 더 거른다.
    """
    w, h = box.x1 - box.x0, box.y1 - box.y0
    if w < min_side_px or h < min_side_px or w * h < min_area_px:
        return True
    if box.x0 / page_w > CORNER_BADGE_X_MIN and box.y1 / page_h < CORNER_BADGE_Y_MAX:
        return True
    return False


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
