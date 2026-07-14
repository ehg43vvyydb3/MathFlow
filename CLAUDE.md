# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 이 프로젝트는 무엇인가

MathFlow는 스캔한 수학 교과서 PDF를 폰에서 읽기 좋게 만들어주는 개인 학습 시스템이다.
컴포넌트 3개가 하나의 공유 JSON 계약으로 이어진다:

```
editor/ (PySide6, 맥)  --분석+보정-->  shared/schema JSON  --rsync-->  server/ (FastAPI, 파이)  --HTTP-->  server/client/ (순수 JS)
```

- **editor**: 무거운 작업(CV 세그멘테이션 + VLM 분류 + 사람 보정)을 전부 담당한다. 파이에서는 절대 안 돈다.
- **server**: 읽기 전용 파일 서버다. `editor`가 파이의 `~/apps/mathflow-server/data/`에 rsync로
  밀어넣은 걸 그대로 서빙만 한다 — 업로드 엔드포인트가 없다. 자기 데이터를 스스로 쓰는 일이 없다.
- **server/client**: 빌드 과정이 없다. 순수 `index.html`/`app.js`/`style.css`를 같은 FastAPI
  프로세스가 `/viewer` 경로로 서빙한다.

`shared/schema/README.md`가 세 컴포넌트 사이의 인터페이스 계약이다 — `book.json`/`pages.json`/
`blocks.json` 형태를 건드리기 전에 먼저 읽을 것. `PLAN.md`는 살아있는 진행 상황 문서(설계 결정,
끝난 것, 남은 것)다 — 의미 있는 작업을 끝내면 지금까지 해왔던 방식대로 여기도 갱신한다.

페이지 원본 이미지는 리포 밖에 있다(`~/Downloads/공통수학2.pdf`); 편집기가 그때그때 렌더링한다.

## 핵심 워크플로: 수작업 → diff → 코드반영 → 재캐싱 루프

이 프로젝트의 실제 개발 사이클은 다음 순서로 돈다. 새 인스턴스는 이 루프를 이해하고 있어야
"사용자가 페이지를 고쳤다"는 말을 듣고 뭘 해야 할지 안다.

```
1. 수작업 보정 (편집기, 사람이 직접)
   타입 변경 / 병합 / 삭제 / 새 블록 그리기 / 리사이즈
   페이지 하나 끝나면 "완료"(단축키 D) 체크 → 저장(S)
        │
        │  단원의 남은 페이지만큼 반복
        ▼
2. 단원 전체 완료
        ▼
3. diff_edits.py 실행
   완료 페이지의 "저장본" ↔ "지금 코드로 다시 돌린 자동분석"을
   IoU(위치 겹침)로 매칭해서 삭제/추가/타입변경 패턴을 건수순으로 집계한 리포트 출력
   (ID 문자열로 매칭하면 세그멘테이션이 바뀐 사이에 ID가 다른 블록을 가리키게 돼서
   가짜 신호가 잡힌다 — 실제로 겪은 문제라 IoU 매칭으로 바꿈)
        ▼
4. 판단 (사람 + Claude가 같이, 자동화하지 않음)
   - 이게 진짜 반복되는 신호인가, 아니면 우연/ID충돌 같은 잡음인가?
     (표본이 단원 하나에 한두 건뿐인 패턴을 성급하게 규칙화하면 과적합 위험)
   - 어디를 고쳐야 하나?
     위치 문제        → segment.py (CV 임계값/규칙, LLM 관여 없음)
     분류 문제(근본)   → vlm_client.py의 PROMPT 문구
     분류 문제(사후)   → pipeline.py의 _apply_type_rules() (LLM 답변을 코드로 덮어씀)
        ▼
5. 코드 수정 + 회귀 검증
   21쪽 손라벨 정답(로컬에만 있음, IoU 커버리지) 등으로 다른 페이지가 안 깨졌는지 확인
        ▼
6. VLM 재실행이 필요하면 "새로 캐싱"
   - segment.py를 고쳤다면: 크롭 이미지 자체가 달라져서 캐시 해시가 자동으로 안 맞는다
     → 그 페이지를 열기만 해도 자동으로 다시 분류된다. 따로 손쓸 필요 없음.
   - PROMPT만 고쳤다면: 크롭은 그대로라 해시도 그대로 → 캐시가 예전 답을 그대로 돌려준다
     → force=True로 캐시를 무시하고 강제로 다시 물어봐야 한다.
     편집기 "도구 → 새로 캐싱" 메뉴 사용 (완료 페이지는 절대 건드리지 않음 —
     미완료 페이지만 대상. "손댔지만 완료 체크는 안 한" 중간 상태로 따로 보호하는 것도
     시도했다가 사용자 요청으로 되돌림 — 완료/미완료 이분법만 쓴다)
        ▼
   (1번으로 돌아가 다음 단원 또는 남은 페이지 계속)
```

## 자주 쓰는 명령어

```bash
# 편집기 최초 설정 (editor/ 안에서, 한 번만)
cd editor && python3 -m venv .venv && source .venv/bin/activate
pip install -e .                          # 의존성 설치 + mathflow_editor를 최상위 이름으로도 매핑
                                           # (prototypes/*.py가 `from mathflow_editor...`로 이 매핑에 의존함 — 지우지 말 것)

# 편집기 실행 (반드시 레포 루트에서, venv는 활성화된 채로)
source editor/.venv/bin/activate
python3 -m editor.mathflow_editor.ui.main # 검토 UI 실행 (PDF 경로·book_id는 ui/main.py에 하드코딩)
                                           # editor/에 __init__.py가 없어 네임스페이스 패키지로 잡힘 →
                                           # 레포 루트가 아닌 곳(예: editor/ 안)에서 실행하면 ModuleNotFoundError

# 스키마 검증
python shared/schema/validate.py                       # shared/schema/examples/*.json 검증
python shared/schema/validate.py editor/output/<book_id>  # 실제 책 산출물 검증

# 완료 페이지 diff 리포트 (루프의 3단계)
python editor/prototypes/diff_edits.py                 # status.json에서 완료 표시된 페이지 전부
python editor/prototypes/diff_edits.py --pages 10-24
python editor/prototypes/diff_edits.py --force          # 캐시 무시하고 실제 VLM 호출 (느림)
# 출력은 stdout에만 찍고 파일로 따로 안 남긴다 — 나중에 다시 보려면 리다이렉트로 직접 저장.
python editor/prototypes/diff_edits.py --pages 10-32 > report.txt

# 서버: 코드 변경사항을 파이에 배포 (data/는 제외 — 그건 편집기가 따로 rsync함)
rsync -avz --exclude data server/ pi:~/apps/mathflow-server/
ssh pi "sudo systemctl restart mathflow-server"
curl http://100.101.163.114:5020/                        # 헬스체크
# 뷰어: http://pi.taildae7bd.ts.net:5020/viewer/ (Tailscale 필요, 공개 Funnel 없음)
```

정식 pytest 스위트는 없다. 이 리포에서 검증은 이렇게 한다:
- `QT_QPA_PLATFORM=offscreen python3 -c "..."` 형태의 즉석 스크립트로 `ReviewWindow`를 헤드리스로
  띄워 `_`로 시작하는 메서드를 직접 호출하고 `win.pages_blocks`/`win.page_status`/저장된 JSON을
  assert한다 (패턴은 최근 커밋들 참고). 뭔가 파일에 쓰는 스크립트라면 `win.output_dir`/
  `win.status_path`를 임시 디렉터리로 먼저 리디렉션해서 `editor/output/`의 실제 책 데이터를
  안 건드리게 한다.
- `node --check server/client/app.js`로 문법 확인, `document`/`localStorage`/`fetch`를 스텁으로
  흉내 낸 작은 Node 스크립트로 순수 로직(`unitForPage`, `goToPage` 등)을 실제 브라우저 없이 검증.
- `book.json`/`pages.json`/`blocks.json`을 쓰는 코드를 건드렸으면 전후로 `shared/schema/validate.py`.

## 아키텍처

### 파이프라인: CV가 위치를 찾고, VLM이 라벨을 붙이고, 코드가 둘 다 교정한다

`editor/mathflow_editor/analysis/`:
- `segment.py` — 순수 OpenCV, ML 없음. 컬럼 분리 → 줄 밴드 투영 → 블록 그룹핑 → 선행 숫자
  라벨 분리(간격 휴리스틱) → 다단 수식 분리(줄높이 휴리스틱) → 잡물 필터 → 패딩. 이 책 특유의
  2단 구성(본문+우측 사이드바)에 맞춰 튜닝돼 있다. `_is_debris`, `SIDEBAR_GAP_MULTIPLIER` 등은
  전부 실제 사용자 보정을 diff해서 뽑아낸 값이지 추측이 아니다.
- `vlm_client.py` — 잘라낸 블록 이미지마다 VLM에 보내 `BLOCK_TYPES` 중 하나로 분류시킨다
  (기본은 로컬 `OllamaBackend`, `qwen2.5vl:7b`; 대안으로 `OpenRouterBackend`도 있고
  `editor/.env`의 `OPENROUTER_API_KEY`가 필요). `confidence` 필드는 **신뢰할 수 없다** — 실측
  422블록에서 거의 항상 ~0.95였다. 이 값으로 로직을 분기하지 말 것.
- `pipeline.py` — 페이지 단위로 위 둘을 묶고(`run_page`) VLM 결과를 **크롭 이미지 바이트의
  SHA256**으로 `BlockCache`에 캐싱한다. `_apply_type_rules()`는 별도의 세 번째 교정 층으로,
  VLM이 뭐라 답하든 상관없이 코드로 결정론적으로 덮어쓴다 (예: 폭 넓은 "problem_number"는
  항상 틀렸으니 무조건 "text"로).
- `review.py` — 기하학적 "검토 필요" 플래그(같은 페이지·같은 타입 대비 유난히 큰 블록 =
  병합 실패 의심). VLM 확신도 대신 이걸 검토 큐 신호로 쓴다.

**캐시 무효화 함정**: `segment.py`를 고치면 잘라내는 바이트 자체가 달라지니 캐시가 자연히
안 맞아서 VLM이 다시 돈다 — 별도 조치 불필요. `PROMPT` 문자열만 고치면 크롭은 그대로라 아무것도
무효화되지 않는다 — 이미 캐싱된 페이지는 계속 예전 답을 돌려받는다. 그래서 `run_page(...,
force=True)`와 편집기의 "새로 캐싱" 메뉴가 존재한다: 프롬프트를 바꾼 게 이미 분석됐던 페이지에도
실제로 반영되게 만드는 유일한 방법이다.

### 사람 보정 → 코드 규칙, 모델 재학습이 아니다

학습 루프는 없다. 의도된 워크플로(위 "핵심 워크플로" 섹션, 그리고 `PLAN.md`의 "핵심 설계 결정"과
최근 커밋 메시지들 참고)는: 사람이 검토 UI에서 블록을 고치고 → 페이지를 "완료"로 표시하고 →
단원 하나가 다 끝나면 `diff_edits.py`가 저장된 보정과 "지금 코드라면 이렇게 냈을 것"을 diff해서
반복 패턴을 찾고 → 그 패턴을 `segment.py`(기하)/`vlm_client.py`의 `PROMPT`(분류)/`pipeline.py`의
`_apply_type_rules`(사후 교정) 중 실제 원인에 맞는 곳에 손으로 코드화한다. 이건 임시방편이
아니라 의도된 설계다: diff 도구 자체는 자동화돼 있지만, *어떤 패턴이 진짜 신호이고 어떤 게
잡음인지, 셋 중 어디를 고칠지* 판단하는 건 계속 사람/Claude의 몫으로 남긴다 — 패턴 하나당
표본이 적어서 무작정 자동 규칙화하면 과적합 위험이 크다 (`diff_edits.py`가 블록을 ID가 아니라
IoU로 매칭하는 것도 이 때문 — 페이지를 보정한 시점과 다시 diff하는 시점 사이에 세그멘테이션
로직이 바뀌면 ID 매칭은 가짜 "변경됨" 신호를 만들어낸다).

페이지 상태는 딱 두 가지, `"done"`이거나 아니거나(`editor/output/<book_id>/status.json`에서
관리, 스키마 검증 대상 파일들과는 별개). "손댔지만 완료 체크는 안 한" 세 번째 상태를 시도했다가
명시적으로 되돌렸다 — 별다른 지시가 없으면 이분법을 유지할 것.

### 지금은 책 하나에 하드코딩돼 있는 게 의도된 상태다

`editor/mathflow_editor/units.py`(단원/페이지범위 표)와 `ui/main.py`(PDF 경로, book_id,
기본 페이지 범위)는 특정 책(개념원리 공통수학2) 하나에 맞춰 하드코딩돼 있다. `server/client/app.js`의
`UNITS`/`BOOK_ID` 상수도 마찬가지 — `units.py`와 손으로 맞춰주고 있을 뿐 코드 공유는 안 된다
(편집기는 Python/Qt, 클라이언트는 브라우저 JS라 둘 사이에 코드 공유 수단이 아직 없다). 두
번째 책을 추가하게 되면 이 하드코딩부터 데이터 기반으로 바꿔야 한다.

### 블록 이미지 파일 없이 되는 리플로우

`server/client/app.js`의 리플로우 화면은 렌더링 시점에 *페이지* 이미지에서 CSS
`background-size`/`background-position` 계산으로 블록 영역만 잘라낸다 (컨테이너 폭에 맞춰
배율을 정하되 `MAX_SCALE`로 상한을 둬서 원본 해상도 이상으로 확대해 흐려지는 걸 막고,
라벨류는 화면 폭 대신 목표 줄높이에 맞추는 `"auto"` 레이아웃 모드를 쓴다). 블록별 이미지
파일을 따로 만들거나 저장하지 않는다 — 페이지당 webp 한 장으로 충분하다. `app.js`의
`ROLE_LAYOUT`은 `pipeline.py`의 `ROLE_BY_TYPE`과 대응 관계를 유지해야 한다.

### 읽는 순서는 배열에 넣은 순서가 아니다

`pages.json`의 `block_order`(그리고 각 블록의 `order` 필드)는 `review_window.py`의 `_save()`
시점에 다시 계산된다 — 컬럼(버킷: `x > 0.5`면 사이드바) 우선, 그 안에서 위→아래 순으로
정렬한다. 메모리상 리스트 순서를 그대로 믿지 않는다. 병합했거나 새로 그린 블록은 생성될 때
항상 리스트 맨 끝에 붙기 때문에, 이 재정렬 없이는 리플로우 순서가 화면상 실제 위치와
조용히 어긋난다.

### 저작권 데이터는 의도적으로 git 밖에 있다

`editor/output/`와 `shared/schema/examples/`(저작권 있는 교재에서 뽑은 실제 페이지 좌표·
메타데이터)는 gitignore돼 있고, 히스토리에서도 아예 지워졌다(첫 퍼블릭 푸시 전에 이걸 위해
리포 히스토리를 한 번 스쿼시했다) — 다시 git에 추가하지 말 것, 새로 뽑은 다른 책 데이터도
마찬가지로 커밋하지 말 것. 페이지 이미지(`editor/output/*/pages/`)와 `status.json`도
편집기 로컬 상태(재생성 가능)라 gitignore 대상이다.
