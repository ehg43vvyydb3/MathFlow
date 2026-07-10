"""책의 단원(대단원-중단원) 구성.

목차(6~8쪽)를 직접 읽어 검증한 페이지 범위다. 다음 단원의 시작 페이지 바로
전까지를 끝 페이지로 잡았다 (예: "1 평면좌표"는 10쪽 시작, "2 직선의 방정식"이
34쪽에서 시작하므로 1 평면좌표는 10~33쪽).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    id: str
    title: str
    start_page: int
    end_page: int

    @property
    def page_range(self) -> range:
        return range(self.start_page, self.end_page + 1)


UNITS: list[Unit] = [
    Unit("I-1", "Ⅰ-1. 평면좌표", 10, 33),
    Unit("I-2", "Ⅰ-2. 직선의 방정식", 34, 65),
    Unit("I-3", "Ⅰ-3. 원의 방정식", 66, 97),
    Unit("I-4", "Ⅰ-4. 도형의 이동", 98, 119),
    Unit("II-1", "Ⅱ-1. 집합의 뜻과 포함 관계", 120, 137),
    Unit("II-2", "Ⅱ-2. 집합의 연산", 138, 165),
    Unit("II-3", "Ⅱ-3. 명제", 166, 205),
    Unit("III-1", "Ⅲ-1. 함수", 206, 247),
    Unit("III-2", "Ⅲ-2. 유리함수", 248, 275),
    Unit("III-3", "Ⅲ-3. 무리함수", 276, 291),
]

UNITS_BY_ID: dict[str, Unit] = {u.id: u for u in UNITS}


def unit_containing(page_number: int) -> Unit | None:
    for u in UNITS:
        if u.start_page <= page_number <= u.end_page:
            return u
    return None
