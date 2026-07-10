"""examples/*.json이 각 *.schema.json을 만족하는지 검증한다.

사용법:
    python validate.py                 # examples/ 검증
    python validate.py <book_dir>      # 실제 산출물 디렉터리 검증 (book/pages/blocks.json)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

SCHEMA_DIR = Path(__file__).parent
FILES = [
    ("book.schema.json", "book.json"),
    ("pages.schema.json", "pages.json"),
    ("blocks.schema.json", "blocks.json"),
]


def validate_dir(data_dir: Path) -> None:
    for schema_name, data_name in FILES:
        schema = json.loads((SCHEMA_DIR / schema_name).read_text())
        data_path = data_dir / data_name
        if not data_path.exists():
            print(f"SKIP: {data_path} 없음")
            continue
        data = json.loads(data_path.read_text())
        jsonschema.validate(data, schema)
        print(f"OK: {data_path} valid against {schema_name}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else SCHEMA_DIR / "examples"
    validate_dir(target)
