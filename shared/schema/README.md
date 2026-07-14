# MathFlow 메타데이터 스키마

편집기(PC) · 서버(Raspberry Pi) · 웹 뷰어가 **공유하는 데이터 계약**이다.
편집기가 이 스키마에 맞춰 파일을 생성하고, 서버는 그대로 서빙하며, 뷰어는 이를 소비한다.

- 편집기: 이 스키마를 **출력**한다. (무거운 분석은 모두 편집기에서)
- 서버: 이 스키마를 **읽어서 API로 제공**한다. (가벼운 서버 역할)
- 뷰어: API가 내려준 이 스키마를 **화면에 표시**한다.

이 문서가 곧 세 컴포넌트의 인터페이스 규격이다. 스키마를 바꾸려면 여기부터 고친다.

> `examples/`는 실제 교재(개념원리 공통수학2) 21쪽에서 뽑은 좌표라 저작권
> 문제로 공개 리포에는 안 올린다(`.gitignore`) — 로컬에는 그대로 있고,
> `validate.py`도 로컬에서는 정상 동작한다. 링크는 로컬 참고용으로 남겨둔다.

---

## 산출물 구조

한 권의 책(book)은 하나의 디렉터리로 산출된다.

```
<book_id>/
├── book.json      # 책·단원(chapter) 메타데이터 (1개)
├── pages.json     # 페이지별 메타데이터
├── blocks.json    # 블록별 메타데이터
├── book.pdf       # 전처리된 최종 PDF
├── pages/         # (선택) 페이지 렌더 이미지 캐시   ex) 0007.webp
├── blocks/        # (선택) 블록 크롭 이미지 캐시      ex) p7_b03.webp
└── thumbs/        # (선택) 페이지 썸네일             ex) 0007.webp
```

이미지 캐시(`pages/`, `blocks/`, `thumbs/`)는 **선택 사항**이다.
없으면 서버가 `book.pdf` + bbox로 실시간 렌더링/크롭한다. 있으면 그걸 우선 서빙해
파이의 CPU 부담을 줄인다. 개발 원칙("메타데이터로 서버 부담 최소화")에 맞춰
편집기에서 미리 생성해 두는 것을 권장한다.

---

## 공통 규약

### 1. 좌표계 — 정규화 좌표 `[0, 1]`

모든 `bbox`는 **페이지 기준 정규화 좌표**다.

- 원점: 페이지 **좌상단**
- x: 오른쪽으로 증가, y: 아래로 증가
- 형식: `[x, y, w, h]` (좌상단 x, 좌상단 y, 너비, 높이) — 모두 `0.0 ~ 1.0`

정규화 좌표를 쓰는 이유:
- 렌더링 DPI/줌 배율과 **무관**하다. 서버가 어떤 해상도로 렌더링해도 동일하게 동작.
- 스캔 PDF의 임의 point 단위에 의존하지 않는다.
- 뷰어 리플로우에서 비율 계산이 간단하다.

픽셀 좌표가 필요하면: `px = bbox[i] * page.width_px` (또는 `height_px`).

### 2. ID 규약

| 대상 | 필드 | 형식 | 예 |
|------|------|------|-----|
| 책 | `book.id` | slug (소문자·숫자·`-`) | `calculus-stewart-8e` |
| 단원 | `chapter.id` | book 내 유일 문자열 | `ch03` |
| 페이지 | `page.number` | 1-indexed 정수 (최종 PDF 순서) | `7` |
| 블록 | `block.id` | book 내 유일 문자열 | `p7_b03` |
| 문제 | `problem.id` | book 내 유일 문자열 | `p7_prob2` |

- `page.number`는 **최종 book.pdf 기준 1-indexed**. 원본 대비 삭제·재배치가 있었다면
  `page.source_number`에 원본 페이지를 별도 보관한다.
- 블록/문제 id는 book 안에서만 유일하면 된다. 권장 패턴 `p{page}_b{n}` / `p{page}_prob{n}`.

### 3. 버전 관리

각 파일 최상위에 `schema_version`(문자열, semver)을 둔다. 현재 **`1.0`**.

- minor 증가(1.0 → 1.1): 하위 호환되는 optional 필드 추가.
- major 증가(1.x → 2.0): 필드 삭제/의미 변경 등 breaking change. 서버가 마이그레이션 담당.

### 4. 시간 형식

모든 타임스탬프는 **ISO 8601 UTC** 문자열. 예) `2026-07-10T09:30:00Z`.

### 5. null vs 필드 생략

- **필수 필드**: 항상 존재해야 한다 (아래 스키마의 `required`).
- **선택 필드**: 값이 없으면 **생략**한다(키 자체를 넣지 않음). `null`은 "분석했으나 값 없음"을
  명시할 때만 사용. (예: OCR을 돌렸으나 텍스트가 비었음 → `"text": ""`; OCR 미실행 → `text` 생략)

---

## 파일별 개요

### book.json
책 1권의 최상위 정보 + 단원(chapter) 목차 + 렌더링 기본값.
자세한 필드는 [`book.schema.json`](book.schema.json), 예시는 [`examples/book.json`](examples/book.json).

### pages.json
페이지별 물리 정보(크기·회전·크롭·기울기 보정)와 그 페이지에 속한 블록/문제 목록.
리플로우 순서의 기준이 되는 `block_order`를 페이지가 보유한다.
[`pages.schema.json`](pages.schema.json) · [`examples/pages.json`](examples/pages.json).

### blocks.json
블록 하나하나의 위치(`bbox`)·종류(`type`)·리플로우 힌트·(선택) 이미지 캐시 경로.
텍스트 블록은 `lines` 배열로 줄 정보를 중첩 보관한다.
[`blocks.schema.json`](blocks.schema.json) · [`examples/blocks.json`](examples/blocks.json).

---

## 블록 타입과 리플로우

`block.type`:

| type | 의미 | 스마트 리플로우 처리 |
|------|------|------|
| `text` | 본문 문단 | 화면 폭에 맞춰 재배치. `lines`로 줄 단위 정보 보유 |
| `formula` | 수식 (독립 행) | 가운데/폭 맞춤. 줄 사이 빈 행이 뚜렷하면(여러 줄 유도 과정 등) `lines`로 줄 단위 분리, 분수·지수처럼 한 줄 안에서 2차원 구조인 부분은 그대로 유지 |
| `figure` | 그림·도형 | 이미지 그대로, 폭 맞춤(축소만) |
| `table` | 표 | 이미지 그대로, 가로 스크롤 허용 |
| `problem_number` | 문제 번호 후보 | 문제 경계 표시에 사용, 보통 인접 블록과 그룹 |
| `page_number` | 쪽 번호 | 편집기가 저장 시 페이지 내 순서를 다시 매길 때 컬럼/위치와 무관하게 항상 그 페이지 `block_order`의 맨 마지막에 둔다 |

리플로우는 OCR이 아니라 **이미지 블록 재배치**가 기본이다.
`page.block_order`에 정의된 순서대로 세로로 쌓고, `block.reflow` 힌트를 참고한다.
OCR 텍스트(`block.text`)는 향후 선택 기능이며, 있으면 뷰어가 진짜 텍스트 리플로우를
할 수 있다.

---

## 확장 예약 (schema_version 1.x에서 추가 예정)

- `block.text`, `block.lines[].text` — OCR 결과
- `block.latex` — 수식 LaTeX 변환 결과 (AI 문제 풀이 연동용)
- `problems.json` 분리 — 문제 자동 인식이 본격화되면 별도 파일로 승격
- `book.language`, `book.grade_level` — 검색/필터

이 필드들은 지금 넣지 않되, 위 규약(선택 필드 생략)에 따라 나중에 추가해도
하위 호환된다.
