"""book.json / pages.json / blocks.json 읽기·쓰기.

`shared/schema/*.schema.json`을 그대로 재사용해 쓰기 전에 항상 검증한다 —
편집기가 스키마를 위반하는 산출물을 만들지 않도록 막는 게 목적이다.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO_ROOT / "shared" / "schema"

_SCHEMA_FILES = {
    "book": "book.schema.json",
    "pages": "pages.schema.json",
    "blocks": "blocks.schema.json",
}


def _load_schema(kind: str) -> dict:
    return json.loads((SCHEMA_DIR / _SCHEMA_FILES[kind]).read_text())


def validate(data: dict, kind: str) -> None:
    """data가 kind("book"/"pages"/"blocks") 스키마를 만족하는지 검사한다.

    위반 시 jsonschema.ValidationError를 그대로 던진다.
    """
    jsonschema.validate(data, _load_schema(kind))


def load(path: Path, kind: str) -> dict:
    data = json.loads(path.read_text())
    validate(data, kind)
    return data


def save(data: dict, path: Path, kind: str) -> None:
    """검증 통과한 데이터만 파일로 쓴다."""
    validate(data, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_book(path: Path) -> dict:
    return load(path, "book")


def save_book(data: dict, path: Path) -> None:
    save(data, path, "book")


def load_pages(path: Path) -> dict:
    return load(path, "pages")


def save_pages(data: dict, path: Path) -> None:
    save(data, path, "pages")


def load_blocks(path: Path) -> dict:
    return load(path, "blocks")


def save_blocks(data: dict, path: Path) -> None:
    save(data, path, "blocks")


# ---------- 페이지별 편집 완료 상태 ----------
# book/pages/blocks 스키마 밖의 편집기 전용 상태라 검증 없이 단순 JSON으로 다룬다.


def load_status(path: Path) -> dict[int, str]:
    """{page_number: "done"|"pending"}. 파일 없으면 빈 dict(전부 미완료 취급)."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {int(k): v for k, v in raw.items()}


def save_status(status: dict[int, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(k): v for k, v in status.items()}, ensure_ascii=False, indent=2) + "\n")


# ---------- 마지막으로 보던 페이지 ----------
# 이 역시 검증 대상 스키마 밖의 편집기 전용 상태다.


def load_last_page(path: Path) -> int | None:
    """마지막으로 열려 있던 페이지 번호. 파일 없으면 None(기본 시작 페이지 사용)."""
    if not path.exists():
        return None
    return json.loads(path.read_text())["page"]


def save_last_page(page: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"page": page}) + "\n")
