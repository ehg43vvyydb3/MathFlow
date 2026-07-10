"""편집기 산출물을 서버로 내보낸다 (렌더링 + rsync).

무거운 연산은 여기(맥)서 다 끝내고, 파이 서버는 결과 파일만 읽어서 서빙하는
가벼운 역할이라는 원칙에 따라 — 페이지 이미지 인코딩까지 로컬에서 끝내고
정적 파일을 그대로 밀어넣는다. 서버 쪽엔 업로드 엔드포인트가 없다.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import cv2

from ..analysis import segment

SSH_HOST = "pi"
REMOTE_DATA_DIR = "~/apps/mathflow-server/data"


def ensure_book_json(
    output_dir: Path, book_id: str, title: str, page_count: int, dpi: int = 150
) -> None:
    """book.json이 없으면 알려진 값으로 새로 만든다 (있으면 손대지 않음)."""
    path = output_dir / "book.json"
    if path.exists():
        return
    data = {
        "schema_version": "1.0",
        "id": book_id,
        "title": title,
        "page_count": page_count,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rendering": {"dpi": dpi, "default_width_px": 1071, "image_format": "webp"},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def render_page_images(
    pdf_path: Path, output_dir: Path, page_numbers: list[int], dpi: int = 150
) -> list[Path]:
    """선택된 페이지들을 webp로 렌더링해 output_dir/pages/ 밑에 저장한다."""
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for page_number in page_numbers:
        img = segment.render_page(pdf_path, page_number - 1, dpi)
        ok, buf = cv2.imencode(".webp", img)
        out_path = pages_dir / f"{page_number:04d}.webp"
        out_path.write_bytes(buf.tobytes())
        written.append(out_path)
    return written


def transfer_to_server(output_dir: Path, book_id: str) -> subprocess.CompletedProcess:
    """book/pages/blocks.json과 pages/*.webp만 골라 rsync로 서버에 올린다.

    (status.json 같은 편집기 전용 파일은 서버가 몰라도 되니 제외)
    """
    remote_dir = f"{REMOTE_DATA_DIR}/{book_id}/"
    subprocess.run(
        ["ssh", SSH_HOST, f"mkdir -p {remote_dir}pages"],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        [
            "rsync",
            "-avz",
            # "*.json"으로 뭉뚱그리면 편집기 전용 status.json까지 걸린다 —
            # 서버로 보낼 세 파일만 이름으로 콕 집어 허용한다.
            "--include=book.json",
            "--include=pages.json",
            "--include=blocks.json",
            "--include=pages/",
            "--include=pages/*.webp",
            "--exclude=*",
            f"{output_dir}/",
            f"{SSH_HOST}:{remote_dir}",
        ],
        capture_output=True,
        text=True,
    )
