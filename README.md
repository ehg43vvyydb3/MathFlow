# MathFlow

> 스캔한 수학 교과서 PDF를 스마트폰에서 읽기 좋게 만들어주는 개인 학습 시스템 (편집기 + 서버 + 웹 뷰어)  
> A personal learning system that turns scanned math-textbook PDFs into a phone-friendly reflowable reader — editor, server, and web viewer.

[English](#english) · [한국어](#한국어)

![python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)
![pyside6](https://img.shields.io/badge/PySide6-6.11-41cd52?logo=qt&logoColor=white)
![fastapi](https://img.shields.io/badge/FastAPI-0.139-009688?logo=fastapi&logoColor=white)
![opencv](https://img.shields.io/badge/OpenCV-5.0-5C3EE8?logo=opencv&logoColor=white)

---

## English

### What it does

Scanned textbook pages are one big image per page. On a phone you end up pinching
and panning across a page that was laid out for A4 paper. MathFlow fixes that by
splitting each page into **blocks** (paragraphs, formulas, figures, tables, problem
numbers, page numbers) and letting the viewer **reflow** those blocks into a single
phone-width column.

There is no OCR. The target PDF is a pure scan with no vector text, so MathFlow
works entirely with image blocks and their coordinates.

```
editor/ (PySide6, Mac)  ──analyze + correct──▶  shared/schema JSON  ──rsync──▶  server/ (FastAPI, Raspberry Pi)  ──HTTP──▶  server/client/ (vanilla JS)
```

All heavy computation happens in the editor on a Mac. The Raspberry Pi only serves
finished files — it never analyzes anything.

### How a page gets processed

Three layers, each fixing what the previous one gets wrong:

| Layer | Tool | Responsibility |
|-------|------|----------------|
| **Where are the blocks?** | OpenCV (`segment.py`) — no ML | Column split → line-band projection → block grouping → label separation → debris filtering |
| **What is each block?** | VLM (`vlm_client.py`) — local Ollama `qwen2.5vl:7b` | Classify each crop as `text` / `figure` / `formula` / `table` / `problem_number` / `page_number` |
| **Fix both** | Code (`pipeline.py` → `_apply_type_rules()`) | Deterministic overrides, applied regardless of what the VLM answered |

VLM results are cached by the **SHA256 of the crop image bytes**, so editing
`segment.py` invalidates the cache automatically (the crops change), while editing
only the prompt does *not* — that's what `force=True` and the editor's
"re-cache" menu are for.

> **The `confidence` field is not trustworthy.** Measured across 422 blocks it was
> almost always ~0.95. Never branch on it. The review queue instead uses a geometric
> heuristic: a block ≥2.5× the median area of same-type blocks on the same page is
> flagged as a probable merge failure.

### The correction loop

The point of the editor isn't to fix pages one by one forever — it's to turn
repeated human corrections into code rules:

```
1. Correct blocks by hand in the review UI, mark pages "done" (D), save (S)
2. Finish a unit
3. python editor/prototypes/diff_edits.py
   → matches saved corrections against a fresh re-run of the current code by IoU
     (not by block ID — segmentation changes make ID matching produce phantom diffs)
   → reports delete / add / type-change patterns ranked by count
4. Decide (human + Claude, deliberately not automated):
   real signal or noise? and which layer is actually at fault?
     position wrong    → segment.py
     type wrong (root) → the PROMPT in vlm_client.py
     type wrong (post) → _apply_type_rules() in pipeline.py
5. Fix the code, check for regressions
6. Re-cache if the prompt changed (crops unchanged → cache would return stale answers)
```

There is **no model training**. Same-book pages reuse a handful of templates, so a
small set of hand-written rules generalizes well — and with only a few samples per
pattern, auto-deriving rules would overfit.

### Components

| Component | Stack | Status |
|---|---|---|
| Metadata schema | JSON Schema (`shared/schema/`) | ✅ v1 frozen, validator included |
| PDF editor | Python, PySide6, OpenCV, PyMuPDF | 🔄 Block analysis + review UI working |
| Server | FastAPI (Raspberry Pi) | 🔄 Deployed; serves book/pages/blocks/images + user-state sync |
| Web viewer | Vanilla HTML/JS, no build step | 🔄 Original & reflow views, page navigation |

`shared/schema/README.md` is the **interface contract** between all three — read it
before touching the shape of `book.json` / `pages.json` / `blocks.json`.

### Quick start

```bash
# Editor — one-time setup
cd editor && python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Editor — run from the repo root (editor/ is a namespace package;
# running from elsewhere raises ModuleNotFoundError)
cd .. && source editor/.venv/bin/activate
python3 -m editor.mathflow_editor.ui.main

# Validate schema output
python shared/schema/validate.py                          # examples
python shared/schema/validate.py editor/output/<book_id>  # real output

# Diff report for completed pages (step 3 of the loop)
python editor/prototypes/diff_edits.py --pages 10-24
python editor/prototypes/diff_edits.py --force            # bypass cache, real VLM calls (slow)

# Deploy the server (data/ is excluded — the editor rsyncs that separately)
rsync -avz --exclude data server/ pi:~/apps/mathflow-server/
ssh pi "sudo systemctl restart mathflow-server"
curl http://<pi-host>:5020/                               # health check
# Viewer: http://<pi-host>:5020/viewer/
```

The editor renders and uploads a unit at a time via its "전송" (send) menu.

### Server API

| Endpoint | Purpose |
|---|---|
| `GET /` | Health check |
| `GET /books` | List books |
| `GET /book/{id}` · `/pages` · `/blocks` | Metadata (the JSON contract) |
| `GET /book/{id}/page/{n}` | Page image (webp) |
| `GET /book/{id}/answers` · `/answer/{n}` | Answer-key pages |
| `GET` · `POST /book/{id}/state` | Cross-device user state (per-key LWW map, SQLite) |

There is no content-upload endpoint. The server never writes its own book data —
the editor pushes it in over rsync.

### Design notes

- **Reflow needs no per-block image files.** The viewer crops blocks out of the
  *page* image at render time with CSS `background-size` / `background-position`.
  One webp per page is enough. `ROLE_LAYOUT` in `app.js` must stay in sync with
  `ROLE_BY_TYPE` in `pipeline.py`.
- **Reading order is recomputed on save**, not taken from array order — column
  first (`x > 0.5` = sidebar bucket), then top-to-bottom. Merged and newly drawn
  blocks always land at the end of the list, so without this the reflow order would
  silently drift from the visual layout.
- **Page status is binary**: `"done"` or not. A third "touched but not confirmed"
  state was tried and explicitly reverted.
- **Single-book hardcoding is intentional for now.** `units.py`, `ui/main.py`, and
  the `UNITS` / `BOOK_ID` constants in `app.js` are pinned to one specific textbook.
  Adding a second book means making these data-driven first.
- **Copyrighted data is deliberately kept out of git.** `editor/output/` and
  `shared/schema/examples/` hold real page coordinates extracted from a copyrighted
  textbook; they are gitignored and were purged from history before the first public
  push. Don't add them back.

### Repo layout

```
editor/                     # PySide6 editor — all heavy lifting
  mathflow_editor/
    analysis/               # segment.py · vlm_client.py · pipeline.py · review.py
    io/                     # export.py · metadata.py
    ui/                     # main.py · review_window.py · flow_layout.py
    units.py                # chapter → page-range table (book-specific)
  prototypes/               # diff_edits.py and other one-off analysis scripts
server/                     # FastAPI app (runs on the Pi)
  client/                   # vanilla web viewer — index.html · app.js · style.css
  sync_store.py             # user-state sync (SQLite)
shared/schema/              # the JSON contract + validator  ← read this first
```

---

## 한국어

### 무엇인가요

스캔한 교과서는 페이지 한 장이 통째로 이미지 하나입니다. A4 기준으로 짜인 지면을
폰에서 보려면 계속 확대하고 밀면서 봐야 합니다. MathFlow는 각 페이지를
**블록**(문단, 수식, 그림, 표, 문제번호, 쪽번호) 단위로 쪼개고, 뷰어가 그 블록들을 폰
화면 폭에 맞춰 **리플로우**해서 한 줄로 쌓아 보여주는 방식으로 이 문제를 해결합니다.

OCR은 쓰지 않습니다. 대상 PDF가 벡터 텍스트가 전혀 없는 순수 스캔본이라, 이미지
블록과 그 좌표만으로 동작합니다.

```
editor/ (PySide6, 맥)  ──분석 + 보정──▶  shared/schema JSON  ──rsync──▶  server/ (FastAPI, 라즈베리파이)  ──HTTP──▶  server/client/ (순수 JS)
```

무거운 연산은 전부 맥의 편집기에서 끝냅니다. 라즈베리파이는 완성된 파일을 서빙만
하고 분석은 일절 하지 않습니다.

### 페이지가 처리되는 방식

세 개의 층이 있고, 뒷 층이 앞 층의 실수를 교정합니다:

| 층 | 도구 | 역할 |
|----|------|------|
| **블록이 어디 있나** | OpenCV (`segment.py`) — ML 없음 | 컬럼 분리 → 줄 밴드 투영 → 블록 그룹핑 → 선행 라벨 분리 → 잡물 필터 |
| **각 블록이 무엇인가** | VLM (`vlm_client.py`) — 로컬 Ollama `qwen2.5vl:7b` | 크롭마다 `text` / `figure` / `formula` / `table` / `problem_number` / `page_number` 중 하나로 분류 |
| **둘 다 교정** | 코드 (`pipeline.py` → `_apply_type_rules()`) | VLM이 뭐라 답하든 코드로 결정론적으로 덮어씀 |

VLM 결과는 **크롭 이미지 바이트의 SHA256**으로 캐싱됩니다. 그래서 `segment.py`를
고치면 크롭 자체가 달라져 캐시가 자동으로 무효화되지만, 프롬프트만 고치면 크롭이
그대로라 무효화가 *안 됩니다* — `force=True`와 편집기의 "새로 캐싱" 메뉴가 그래서
존재합니다.

> **`confidence` 필드는 신뢰할 수 없습니다.** 422블록 실측에서 거의 항상 ~0.95였습니다.
> 이 값으로 로직을 분기하지 마세요. 검토 큐는 대신 기하 휴리스틱을 씁니다 — 같은
> 페이지·같은 타입 블록의 중앙값 면적 대비 2.5배 이상이면 병합 실패로 의심하고
> "검토 필요"로 표시합니다.

### 보정 루프

편집기의 목적은 페이지를 하나하나 영원히 고치는 게 아니라, 반복되는 사람의 보정을
코드 규칙으로 환원하는 것입니다:

```
1. 검토 UI에서 손으로 블록 보정 → 페이지 "완료" 체크(D) → 저장(S)
2. 단원 하나 완료
3. python editor/prototypes/diff_edits.py
   → 저장된 보정본과 "지금 코드로 다시 돌린 결과"를 IoU로 매칭
     (ID로 매칭하면 세그멘테이션이 바뀐 사이에 가짜 "변경됨" 신호가 잡힘)
   → 삭제 / 추가 / 타입변경 패턴을 건수순으로 리포트
4. 판단 (사람 + Claude가 같이, 의도적으로 자동화하지 않음):
   진짜 신호인가 잡음인가? 그리고 어느 층이 실제 원인인가?
     위치 문제        → segment.py
     분류 문제(근본)  → vlm_client.py의 PROMPT
     분류 문제(사후)  → pipeline.py의 _apply_type_rules()
5. 코드 수정 + 회귀 검증
6. 프롬프트를 고쳤다면 새로 캐싱 (크롭이 그대로라 캐시가 옛 답을 돌려줌)
```

**모델 재학습은 없습니다.** 같은 책은 몇 종류의 템플릿이 반복되므로 손으로 쓴 소수의
규칙으로 잘 일반화되고, 패턴 하나당 표본이 적어서 규칙을 자동으로 뽑으면 과적합
위험이 큽니다.

### 구성 요소

| 구성 요소 | 기술 | 상태 |
|---|---|---|
| 메타데이터 스키마 | JSON Schema (`shared/schema/`) | ✅ v1 확정, 검증 스크립트 포함 |
| PDF 편집기 | Python, PySide6, OpenCV, PyMuPDF | 🔄 블록 분석 + 검토 UI 동작 중 |
| 서버 | FastAPI (라즈베리파이) | 🔄 배포됨. book/pages/blocks/이미지 서빙 + 사용자 상태 동기화 |
| 웹 뷰어 | 순수 HTML/JS, 빌드 과정 없음 | 🔄 원본·리플로우 보기, 페이지 이동 동작 |

`shared/schema/README.md`가 세 컴포넌트 사이의 **인터페이스 계약**입니다 —
`book.json` / `pages.json` / `blocks.json`의 형태를 건드리기 전에 먼저 읽으세요.

### 빠른 시작

```bash
# 편집기 — 최초 1회 설정
cd editor && python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 편집기 — 반드시 레포 루트에서 실행 (editor/는 네임스페이스 패키지라
# 다른 경로에서 실행하면 ModuleNotFoundError)
cd .. && source editor/.venv/bin/activate
python3 -m editor.mathflow_editor.ui.main

# 스키마 검증
python shared/schema/validate.py                          # 예제
python shared/schema/validate.py editor/output/<book_id>  # 실제 산출물

# 완료 페이지 diff 리포트 (루프의 3단계)
python editor/prototypes/diff_edits.py --pages 10-24
python editor/prototypes/diff_edits.py --force            # 캐시 무시, 실제 VLM 호출 (느림)

# 서버 배포 (data/는 제외 — 그건 편집기가 따로 rsync함)
rsync -avz --exclude data server/ pi:~/apps/mathflow-server/
ssh pi "sudo systemctl restart mathflow-server"
curl http://<pi-host>:5020/                               # 헬스체크
# 뷰어: http://<pi-host>:5020/viewer/
```

편집기의 "전송" 메뉴로 단원 단위로 렌더링·업로드합니다.

### 서버 API

| 엔드포인트 | 용도 |
|---|---|
| `GET /` | 헬스체크 |
| `GET /books` | 책 목록 |
| `GET /book/{id}` · `/pages` · `/blocks` | 메타데이터 (JSON 계약) |
| `GET /book/{id}/page/{n}` | 페이지 이미지 (webp) |
| `GET /book/{id}/answers` · `/answer/{n}` | 정답지 페이지 |
| `GET` · `POST /book/{id}/state` | 기기 간 사용자 상태 (항목별 LWW 맵, SQLite) |

콘텐츠 업로드 엔드포인트는 없습니다. 서버가 자기 책 데이터를 직접 쓰는 일은 없고,
편집기가 rsync로 밀어 넣습니다.

### 설계 노트

- **리플로우에 블록별 이미지 파일이 필요 없습니다.** 뷰어가 렌더링 시점에 CSS
  `background-size` / `background-position` 계산으로 *페이지* 이미지에서 블록 영역만
  잘라냅니다. 페이지당 webp 한 장이면 충분합니다. `app.js`의 `ROLE_LAYOUT`은
  `pipeline.py`의 `ROLE_BY_TYPE`과 대응 관계를 유지해야 합니다.
- **읽는 순서는 저장 시점에 재계산됩니다.** 배열에 넣은 순서를 그대로 믿지 않고,
  컬럼 우선(`x > 0.5`이면 사이드바 버킷) → 그 안에서 위→아래로 정렬합니다. 병합했거나
  새로 그린 블록은 항상 리스트 맨 끝에 붙기 때문에, 이 재정렬이 없으면 리플로우 순서가
  실제 화면 위치와 조용히 어긋납니다.
- **페이지 상태는 이분법입니다**: `"done"`이거나 아니거나. "손댔지만 완료 체크는 안 한"
  세 번째 상태를 시도했다가 명시적으로 되돌렸습니다.
- **책 하나에 하드코딩된 것은 지금은 의도된 상태입니다.** `units.py`, `ui/main.py`,
  그리고 `app.js`의 `UNITS` / `BOOK_ID` 상수가 특정 교재 하나에 맞춰져 있습니다. 두 번째
  책을 추가하려면 이 하드코딩부터 데이터 기반으로 바꿔야 합니다.
- **저작권 데이터는 의도적으로 git 밖에 있습니다.** `editor/output/`와
  `shared/schema/examples/`는 저작권 있는 교재에서 뽑은 실제 페이지 좌표라 gitignore돼
  있고, 첫 퍼블릭 푸시 전에 히스토리에서도 지웠습니다. 다시 추가하지 마세요.

### 레포 구조

```
editor/                     # PySide6 편집기 — 무거운 작업 전담
  mathflow_editor/
    analysis/               # segment.py · vlm_client.py · pipeline.py · review.py
    io/                     # export.py · metadata.py
    ui/                     # main.py · review_window.py · flow_layout.py
    units.py                # 단원 → 페이지범위 표 (책별)
  prototypes/               # diff_edits.py 등 일회성 분석 스크립트
server/                     # FastAPI 앱 (파이에서 구동)
  client/                   # 순수 웹 뷰어 — index.html · app.js · style.css
  sync_store.py             # 사용자 상태 동기화 (SQLite)
shared/schema/              # JSON 계약 + 검증 스크립트  ← 여기부터 읽으세요
```
