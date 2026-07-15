"""MathFlow 서버 — book/pages/blocks 메타데이터와 페이지 이미지를 서빙한다.

무거운 연산(전처리·분석)은 전부 편집기(맥) 쪽에서 끝내고, 여기는 결과물을
읽어서 내려주기만 하는 가벼운 서버다. 책 콘텐츠 데이터는 편집기의 "서버로 전송"
메뉴가 rsync로 밀어넣는다 (이 서버가 콘텐츠 업로드를 받는 게 아니라, data/ 밑에
이미 있는 파일을 읽기만 한다).

유일한 예외는 사용자 상태(학습기록·즐겨찾기·북마크 등) 기기 간 동기화다 —
이건 뷰어가 여러 기기에서 공유해야 해서 작은 쓰기 저장소(data/sync.db)가
필요하다. /book/{id}/state GET/POST로만 노출하고, 콘텐츠(book/pages/blocks/
이미지)는 여전히 읽기 전용이다. 상세는 sync_store.py 참고.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import sync_store

DATA_DIR = Path(__file__).parent / "data"
CLIENT_DIR = Path(__file__).parent / "client"
SYNC_DB_PATH = DATA_DIR / "sync.db"

app = FastAPI(title="MathFlow Server")


# 빌드 없는 순수 JS 뷰어라 app.js/style.css가 자주 바뀐다. no-cache를 붙여
# 브라우저가 캐시를 쓰기 전에 항상 etag로 재검증하게 한다 — 안 바뀌었으면 304라
# 트래픽 부담이 없고, 배포/rsync 직후 바로 반영된다("배포했는데 안 보임" 방지).
@app.middleware("http")
async def no_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


def _book_dir(book_id: str) -> Path:
    d = DATA_DIR / book_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"book '{book_id}' not found")
    return d


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{path.name} not found")
    return json.loads(path.read_text())


@app.get("/")
def health() -> dict:
    return {"status": "ok", "service": "mathflow-server"}


@app.get("/books")
def list_books() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    books = []
    for d in sorted(DATA_DIR.iterdir()):
        book_json = d / "book.json"
        if book_json.exists():
            books.append(_load_json(book_json))
    return books


@app.get("/book/{book_id}")
def get_book(book_id: str) -> dict:
    return _load_json(_book_dir(book_id) / "book.json")


@app.get("/book/{book_id}/pages")
def get_pages(book_id: str) -> dict:
    return _load_json(_book_dir(book_id) / "pages.json")


@app.get("/book/{book_id}/blocks")
def get_blocks(book_id: str) -> dict:
    return _load_json(_book_dir(book_id) / "blocks.json")


@app.get("/book/{book_id}/page/{page_number}")
def get_page_image(book_id: str, page_number: int) -> FileResponse:
    book_dir = _book_dir(book_id)
    for ext in ("webp", "png", "jpg"):
        candidate = book_dir / "pages" / f"{page_number:04d}.{ext}"
        if candidate.exists():
            return FileResponse(candidate)
    raise HTTPException(status_code=404, detail=f"page {page_number} image not found")


# 답지(정답 및 풀이): 교재페이지→답지페이지 매핑(answers.json)과 답지 페이지 이미지.
# 답지가 없는 책은 answers.json이 없어 404 — 뷰어는 그 경우 "답 보기"를 숨긴다.
@app.get("/book/{book_id}/answers")
def get_answers(book_id: str) -> dict:
    return _load_json(_book_dir(book_id) / "answers.json")


@app.get("/book/{book_id}/answer/{page_number}")
def get_answer_image(book_id: str, page_number: int) -> FileResponse:
    book_dir = _book_dir(book_id)
    for ext in ("webp", "png", "jpg"):
        candidate = book_dir / "answers" / f"{page_number:04d}.{ext}"
        if candidate.exists():
            return FileResponse(candidate)
    raise HTTPException(status_code=404, detail=f"answer page {page_number} image not found")


# 사용자 상태 동기화. 콘텐츠와 달리 여기만 쓰기가 가능하다 — 여러 기기가 공유하는
# 학습기록/즐겨찾기/북마크/최근/마지막페이지/답지분할선을 항목 단위 LWW로 병합한다.
# sync 핸들러는 blocking sqlite를 부르므로 async가 아닌 def로 둬서 스레드풀에서
# 돌게 한다(이벤트 루프를 막지 않게). book_id는 리터럴 세그먼트 "state"라
# /book/{id}/pages·blocks·answers·page/{n}·answer/{n} 어느 것과도 겹치지 않는다.
def _valid_book_id(book_id: str) -> str:
    try:
        return sync_store.validate_book_id(book_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid book_id")


@app.get("/book/{book_id}/state")
def get_state(book_id: str) -> dict:
    bid = _valid_book_id(book_id)
    return {"schema": 1, "state": sync_store.load_state(SYNC_DB_PATH, bid)}


@app.post("/book/{book_id}/state")
def post_state(book_id: str, body: dict = Body(default={})) -> dict:
    bid = _valid_book_id(book_id)
    incoming = body.get("state") if isinstance(body, dict) else None
    merged = sync_store.merge_state(SYNC_DB_PATH, bid, incoming or {})
    return {"schema": 1, "state": merged}


# 빌드 없는 순수 HTML/JS 뷰어. API 라우트 뒤에 등록해야 이 마운트가
# "/book/..." 같은 API 경로를 가로채지 않는다.
app.mount("/viewer", StaticFiles(directory=CLIENT_DIR, html=True), name="viewer")
