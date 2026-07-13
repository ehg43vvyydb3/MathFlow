"""답지(정답 및 풀이) PDF를 뷰어용 자산으로 빌드한다.

답지는 리플로우가 필요 없다 — 풀고 나서 확인하는 용도라 원본 페이지 이미지
그대로 보면 된다. 그래서 세그멘테이션/VLM 없이 페이지를 webp로만 렌더하고,
각 페이지 상단의 "본책 X~Y쪽" 헤더를 tesseract로 읽어 교재페이지→답지페이지
매핑(answers.json)을 만든다.

산출물 (editor/output/<book_id>/ 밑, 기존 pages/·blocks.json 등과 별개):
  answers/NNNN.webp   답지 PDF 각 페이지 (1-indexed, /book/{id}/answer/{n}로 서빙)
  answers.json        { schema_version, count, page_map: {교재페이지: 답지페이지} }

헤더 OCR은 스캔본이라 절반 정도만 잡히지만, 답지 각 페이지가 "본책 X~Y쪽"을
자기 표시하므로 ±1쪽 오차는 사용자가 바로 확인·보정할 수 있다. 잡힌 앵커를
단조 정제한 뒤 교재 전 페이지에 대해 선형 보간해 map을 채운다.

실행:
  python editor/prototypes/build_answers.py
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import cv2
import fitz
import numpy as np

ANSWER_PDF = Path.home() / "Downloads" / "공통수학2_정답.pdf"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "gongtong-math-2"
DPI = 150
# 교재 페이지 범위 (units.py 기준: 10~291)
BOOK_MIN, BOOK_MAX = 10, 291


def render_answers(doc: fitz.Document, answers_dir: Path) -> tuple[int, int, int]:
    """답지 PDF 각 페이지를 answers/NNNN.webp(1-indexed)로 저장.
    (페이지 수, 첫 페이지 폭px, 첫 페이지 높이px) 반환 — 뷰어 2단 분할용 종횡비."""
    answers_dir.mkdir(parents=True, exist_ok=True)
    page_w = page_h = 0
    for i in range(doc.page_count):
        pix = doc[i].get_pixmap(dpi=DPI)
        if i == 0:
            page_w, page_h = pix.width, pix.height
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".webp", img)
        (answers_dir / f"{i + 1:04d}.webp").write_bytes(buf.tobytes())
    return doc.page_count, page_w, page_h


def ocr_anchors(doc: fitz.Document) -> list[tuple[int, int, int]]:
    """각 페이지 상단 "본책 X~Y쪽" 헤더를 읽어 (답지페이지1indexed, from, to) 앵커.

    단조(비감소)로 정제해 OCR 이상치를 걸러낸다.
    """
    raw: list[tuple[int, int, int]] = []
    with tempfile.TemporaryDirectory() as tmp:
        strip = Path(tmp) / "h.png"
        for i in range(doc.page_count):
            r = doc[i].rect
            pix = doc[i].get_pixmap(dpi=260, clip=fitz.Rect(0, 0, r.width, r.height * 0.075))
            pix.save(strip)
            out = subprocess.run(
                ["tesseract", str(strip), "-", "--psm", "7",
                 "-c", "tessedit_char_whitelist=0123456789~-"],
                capture_output=True, text=True,
            ).stdout
            m = re.search(r"(\d{1,3})\s*[~-]\s*(\d{1,3})", out)
            if not m:
                continue
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and b - a <= 15 and BOOK_MIN <= a <= BOOK_MAX + 4:
                raw.append((i + 1, a, b))
    anchors: list[tuple[int, int, int]] = []
    prev = 0
    for page, a, b in raw:
        if a >= prev:  # from이 뒤로 튀는 이상치 제거
            anchors.append((page, a, b))
            prev = a
    return anchors


def build_page_map(anchors: list[tuple[int, int, int]], count: int) -> dict[str, int]:
    """교재 페이지 → 답지페이지 map. 앵커 "본책 X~Y" 범위는 그대로 채우고,
    앵커가 없는 교재 페이지(홀수 페이지 OCR 실패 등)만 선형 보간으로 메운다."""
    # 앵커 범위를 직접 채운다 — 겹치는 경계는 더 이른(작은) 답지페이지 우선
    # (그 페이지 문제의 답이 시작되는 쪽).
    covered: dict[int, int] = {}
    for page, a, b in anchors:
        for p in range(a, b + 1):
            if p not in covered or page < covered[p]:
                covered[p] = page
    known = sorted(covered)
    page_map: dict[str, int] = {}
    for p in range(BOOK_MIN, BOOK_MAX + 1):
        if p in covered:
            ans = covered[p]
        else:
            below = [k for k in known if k < p]
            above = [k for k in known if k > p]
            if below and above:
                b0, b1 = below[-1], above[0]
                a0, a1 = covered[b0], covered[b1]
                t = (p - b0) / (b1 - b0)
                ans = round(a0 + t * (a1 - a0))
            elif below:  # 모든 앵커보다 뒤 → 가장 가까운 아래(최대 known)
                ans = covered[below[-1]]
            else:  # 모든 앵커보다 앞 → 가장 가까운 위(최소 known)
                ans = covered[above[0]]
        page_map[str(p)] = max(1, min(count, int(ans)))
    return page_map


def main() -> None:
    assert ANSWER_PDF.exists(), f"답지 PDF 없음: {ANSWER_PDF}"
    doc = fitz.open(ANSWER_PDF)
    print(f"답지 {doc.page_count}쪽 렌더링...")
    count, page_w, page_h = render_answers(doc, OUTPUT_DIR / "answers")
    print("헤더 OCR로 앵커 추출...")
    anchors = ocr_anchors(doc)
    print(f"  앵커 {len(anchors)}개 (예: {anchors[:3]} ... {anchors[-2:]})")
    page_map = build_page_map(anchors, count)
    answers_json = {
        "schema_version": "1.0",
        "count": count,
        "page_w": page_w,
        "page_h": page_h,
        "page_map": page_map,
    }
    out = OUTPUT_DIR / "answers.json"
    out.write_text(json.dumps(answers_json, ensure_ascii=False, indent=2) + "\n")
    print(f"완료: answers/*.webp {count}장, {out}")
    print(f"  샘플 매핑: 10쪽→{page_map['10']}  42쪽→{page_map['42']}  200쪽→{page_map['200']}  291쪽→{page_map['291']}")


if __name__ == "__main__":
    main()
