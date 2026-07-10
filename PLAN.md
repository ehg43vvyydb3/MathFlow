# MathFlow — 개인 수학 PDF 학습 서버

수학책 PDF(스캔본)를 스마트폰 브라우저에서 읽기 좋게 만들어주는 개인 학습 시스템.
무거운 연산(전처리·분석)은 PC에서 전부 끝내고, 라즈베리파이는 결과만 서빙한다.
휴대폰에는 앱을 설치하지 않는다 — 브라우저만 사용.

```
PC (편집기: 분석·보정) ──▶ book.pdf + 메타데이터(JSON) ──▶ Raspberry Pi (FastAPI) ──▶ 폰 브라우저 (웹 뷰어)
```

## 구성 요소

| 구성 요소 | 기술 | 상태 |
|---|---|---|
| 메타데이터 스키마 | JSON Schema (`shared/schema/`) | ✅ v1 확정, 검증 스크립트 포함 |
| PDF 편집기 | Python, PySide6, OpenCV, PyMuPDF | 🔄 블록 분석 + 검토 UI 동작 중 |
| 서버 | FastAPI (라즈베리파이) | 🔄 배포됨, book/pages/blocks/이미지 서빙만 (DB·북마크 등은 아직) |
| 웹 뷰어 | 순수 HTML/JS (빌드 없음) | 🔄 원본·리플로우 보기, 페이지 이동 동작. 북마크 등은 localStorage |

편집기는 맥 로컬, 서버는 이미 라즈베리파이(`~/apps/mathflow-server`)에 올라가 있다.
편집기의 "전송" 메뉴로 단원 단위로 렌더링·업로드한다 (rsync, SSH 호스트 별칭 `pi`).
웹 뷰어는 서버가 `/viewer` 경로로 같이 서빙한다 — 원래 계획에 있던 PDF.js는 안 쓴다
(편집기에서 이미 페이지를 이미지로 렌더링해서 넘기므로 브라우저에서 PDF를 다시 파싱할 필요가 없다).

## 핵심 설계 결정 (검증 완료)

- **OCR 없이 이미지 블록 기반**으로 처리한다. 대상 PDF(개념원리 공통수학2, 304쪽)는
  페이지 전체가 스캔 이미지라 벡터 텍스트가 없음을 확인했다.
- **블록 위치는 CV**(투영 기반 세그멘테이션, `segment.py`),
  **블록 타입은 VLM**(로컬 Ollama `qwen2.5vl:7b`, 무료·오프라인)으로 나눠 맡긴다.
  블록 타입: text / figure(그림·그래프) / formula / table / problem_number.
- **VLM 자체 확신도는 신뢰도 신호로 쓸 수 없다** (24페이지 422블록 실측: 항상 ~0.95).
  대신 기하 휴리스틱(같은 페이지·같은 타입 대비 면적 2.5배 이상 = 병합 실패 의심)으로
  "검토 필요" 블록을 표시한다.
- **사람 보정은 규칙으로 환원한다**: 검토 UI에서 사용자가 고친 내역을 자동 분석과
  diff해서 반복 패턴을 찾고, 세그멘테이션/분류 규칙으로 코드에 반영한다.
  같은 책은 템플릿 몇 종류가 반복되므로 소수의 규칙으로 잘 일반화된다.
  - 지금까지 반영된 규칙: 문제번호 선행 라벨 분리(간격 신호), 다단 수식 분리(줄 높이 신호),
    미세 잡물 제거(장식 괘선·점 조각), 폭 넓은 problem_number → text 교정, bbox 3px 패딩.

## 디렉터리

```
shared/schema/     book/pages/blocks JSON 스키마 + 예시 + validate.py
editor/
  mathflow_editor/
    analysis/      segment.py(CV 위치) vlm_client.py(타입) pipeline.py(결합+캐시) review.py(검토 플래그)
    io/            metadata.py (스키마 검증 읽기/쓰기), export.py (렌더링+rsync 전송)
    ui/            review_window.py (PySide6 검토·보정 UI), main.py (진입점)
    units.py       단원(대단원-중단원) 페이지 범위 — 목차 기준으로 검증된 10개 단원
  prototypes/      단계별 실험 스크립트 (eval_page21, batch_classify, analyze_chapter)
  projects/        책별 VLM 캐시 (git 제외)
  output/          편집기 산출물: book/pages/blocks.json, status.json(완료 표시, git 제외)
server/            FastAPI 서버 소스 (파이의 ~/apps/mathflow-server 로 rsync 배포)
  main.py          book/pages/blocks.json + 페이지 이미지 서빙만 (data/ 밑 정적 파일 read-only),
                    /viewer 경로에 client/ 정적 서빙도 같이 붙임
  client/          웹 뷰어 (index.html/app.js/style.css, 빌드 없음). 북마크·즐겨찾기·
                    최근 페이지는 localStorage — book_id로만 네임스페이스, 서버 동기화 없음
  mathflow-server.service   systemd 유닛 (파이에 설치됨, 상시 실행 + monitor 앱 등록)
```

편집기 실행: `editor/.venv` 활성화 후 `python -m mathflow_editor.ui.main`
서버 배포(코드 변경 시): `rsync -avz --exclude data server/ pi:~/apps/mathflow-server/` 후
`ssh pi "sudo systemctl restart mathflow-server"`. 헬스체크: `curl http://100.101.163.114:5020/`.
뷰어 접속: `http://pi.taildae7bd.ts.net:5020/viewer/` (tailnet 연결 필요, Funnel 없음).
파이는 Tailscale tailnet 안에서만 접근 가능(Funnel 없음) — 폰 브라우저는 tailnet 연결 상태에서 접속.

## 진행 상황

- [x] 메타데이터 스키마 v1 (book/pages/blocks) + 검증
- [x] CV 세그멘테이션: 컬럼 분리 → 줄 밴드 → 블록 그룹핑 → 라벨/수식 분리 → 잡물 필터
- [x] VLM 타입 분류 + 이미지 해시 캐싱 (재실행 시 재추론 없음)
- [x] 검토 UI: 타입 변경(선택 동기화), 병합, 삭제, 새 블록 그리기, 가장자리 리사이즈,
      범례(L 토글), 검토 필요 점프, 스키마 검증 저장, 저장 확인(저장/저장안함/취소) + Ctrl+S
- [x] 단원 열기 메뉴 (목차로 검증된 10개 단원), 페이지별 완료 표시(D 토글)
- [x] 새로 캐싱 메뉴 2종: 단원 내 완료→미완료 / 완료 단원 → 다른 단원 (force 재분류)
- [x] "1. 평면좌표" 단원(10~33쪽) 자동 분석 — 사용자 보정 진행 중 (10~18, 23쪽 완료)
- [x] 라즈베리파이에 FastAPI 서버 배포 (`~/apps/mathflow-server`, systemd 상시 서비스,
      monitor 앱 등록, tailnet 전용 — Funnel 없음)
- [x] 편집기 "전송" 메뉴: 단원 선택 → 페이지 렌더링(webp) → rsync로 서버 업로드,
      실제 API 응답까지 확인함
- [x] 웹 뷰어 (빌드 없는 순수 HTML/JS, `server/client/`): 원본 보기(네이티브 핀치줌),
      스마트 리플로우(블록 이미지를 따로 안 만들고 CSS background-crop으로 재배치),
      페이지 이동, 단원 이동, 북마크·즐겨찾기·최근 페이지(전부 localStorage —
      서버 DB 아직 없음, 기기 간 동기화 안 됨). `/viewer` 경로로 서빙, 10~24쪽 실제 전송 완료.
      접속: `http://pi.taildae7bd.ts.net:5020/viewer/` (tailnet 필요)
- [ ] 단원 나머지 페이지 보정 → 편집 diff 분석 → 규칙 반영 (반복)
- [ ] problem 그룹핑 (문제번호 ↔ 소속 블록 연결, pages.json의 problems 채우기)
- [ ] 서버: SQLite로 북마크·즐겨찾기·학습기록 (지금은 정적 파일 서빙만)
- [ ] 웹 뷰어 (페이지 보기, 블록 보기, 스마트 리플로우, 문제 단위 이동)

## 향후 확장 (원 계획서에서)

문제 자동 인식 고도화, 단원/문제 검색, AI 문제 풀이 연동, 오답노트, 학습 통계,
다크모드, 다중 사용자, 클라우드 동기화.
