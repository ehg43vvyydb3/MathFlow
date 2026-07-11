"""책의 단원(대단원-중단원) 구성.

목차(6~8쪽)를 읽어 검증한 페이지 범위다. 목차상 각 소단원의 "01" 페이지
번호를 기준으로 다음 단원의 시작 바로 전까지를 끝 페이지로 잡되, 그
경계에 다음 단원 표지(장 도입부 그림+제목만 있는 페이지)가 끼어 있으면
표지가 속한 다음 단원 쪽으로 옮긴다 — 예: 33쪽은 목차 페이지 번호로는
"1 평면좌표"(10쪽 시작) 범위에 들어가지만 실제로는 "2 직선의 방정식"
표지라서 Ⅰ-2에 포함시켰다 (사용자 확인, 2026-07-11). 다른 경계도
이런 표지 페이지가 끼어 있을 수 있으니 편집하다 발견하면 알려줄 것.
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
    Unit("I-1", "Ⅰ-1. 평면좌표", 10, 32),
    Unit("I-2", "Ⅰ-2. 직선의 방정식", 33, 65),
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
