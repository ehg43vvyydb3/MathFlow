// MathFlow 웹 뷰어 — 빌드 없는 순수 JS. 서버는 book/pages/blocks.json과 페이지
// 이미지를 읽기 전용으로 서빙한다. 사용자 상태(북마크/즐겨찾기/최근/문제표시/
// 마지막페이지/답지분할선)는 로컬 localStorage 원장에 저장하면서, 동시에 서버의
// 작은 동기화 저장소(/book/{id}/state)와 항목 단위 LWW로 병합해 기기 간 동기화한다
// (아래 "기기 간 동기화" 섹션). 서버가 없거나 오프라인이면 로컬만으로 동작한다.

const BOOK_ID = "gongtong-math-2";
const API = ""; // 같은 오리진에서 서빙되므로 상대경로

// 페이지 범위는 editor/mathflow_editor/units.py의 UNITS와 손으로 맞춘다(코드
// 공유 수단 없음). 2026-07-14: 소단원 표지가 다음 단원 대신 이전 단원 마지막
// 페이지로 오귀속되던 버그가 Ⅰ-2 이후 모든 경계에 반복돼 있어 전체 재검증
// 후 수정 — 자세한 근거는 units.py 쪽 docstring 참고.
const UNITS = [
  { id: "I-1", title: "Ⅰ-1. 평면좌표", start: 10, end: 32 },
  { id: "I-2", title: "Ⅰ-2. 직선의 방정식", start: 33, end: 64 },
  { id: "I-3", title: "Ⅰ-3. 원의 방정식", start: 65, end: 96 },
  { id: "I-4", title: "Ⅰ-4. 도형의 이동", start: 97, end: 118 },
  { id: "II-1", title: "Ⅱ-1. 집합의 뜻과 포함 관계", start: 119, end: 136 },
  { id: "II-2", title: "Ⅱ-2. 집합의 연산", start: 137, end: 164 },
  { id: "II-3", title: "Ⅱ-3. 명제", start: 165, end: 204 },
  { id: "III-1", title: "Ⅲ-1. 함수", start: 205, end: 246 },
  { id: "III-2", title: "Ⅲ-2. 유리함수", start: 247, end: 274 },
  { id: "III-3", title: "Ⅲ-3. 무리함수", start: 275, end: 291 },
];

const ROLE_LAYOUT = {
  label: { mode: "auto", targetHeightPx: 26 },
  paragraph: { mode: "full" },
  equation: { mode: "full" },
  figure: { mode: "full" },
  table: { mode: "full" },
  page_number: { mode: "auto", targetHeightPx: 22 },
};

// 페이지는 150dpi로 렌더링돼 있다. 이보다 훨씬 크게 늘리면 원본에 없는
// 디테일을 억지로 만들어내는 셈이라 흐려진다 — 2배 정도가 실용적인 상한.
const MAX_SCALE = 2.0;
// 글자는 그림과 달리 좀 확대해도 알아보는 데 지장이 덜하다(원래 인쇄된 글자라
// 획 굵기가 단순함) — text 블록만 상한을 더 풀어서, 좁은 사이드바 문단도
// 화면 폭에 가깝게 키운다.
const TEXT_MAX_SCALE = 3.2;

const state = {
  book: null,
  pagesByNumber: new Map(), // number -> {number, width_px, height_px, block_order}
  blocksById: new Map(),
  currentPage: 1,
  unit: UNITS[0], // 뷰어도 편집기처럼 단원 단위로 열어서 본다 — 페이지 이동은 이 단원 범위 안에서만
  viewMode: "reflow", // "image" | "reflow"
  answers: null, // answers.json { count, page_w, page_h, page_map } — 답지 없는 책이면 null
  answerPage: 1, // 답지 모달에서 현재 보고 있는 답지 페이지(1-indexed)
  answerMode: "split", // "split"(2단 세로 분할) | "full"(통짜 페이지)
  answerSplits: {}, // {답지페이지: 거터x} 동기화 원장에서 파생 (rebuildDerived)
  marks: {}, // {"페이지:문제순번": "done"|"important"} 동기화 원장에서 파생 (rebuildDerived)
};

function unitForPage(page) {
  return UNITS.find((u) => page >= u.start && page <= u.end) || UNITS[0];
}

// ---------- 로컬 저장 키 (책 단위 네임스페이스) ----------

function lsKey(kind) {
  return `mathflow.${kind}.${BOOK_ID}`;
}

function lsGetList(kind) {
  try {
    return JSON.parse(localStorage.getItem(lsKey(kind))) || [];
  } catch {
    return [];
  }
}

// ---------- 기기 간 동기화 (항목별 Last-Write-Wins 맵) ----------
// 모든 사용자 상태(문제표시·즐겨찾기·북마크·최근·마지막페이지·답지분할선)를 항목
// 단위 LWW 맵으로 관리한다. 각 항목 = {v:값, t:타임스탬프(ms), d:삭제(tombstone)}.
// 로컬은 localStorage(lsKey("sync"))에 원장으로 저장하고, 변경 시 서버로 push,
// 로드/포커스 시 pull해 같은 규칙(더 최신 t가 이김)으로 병합한다. 서버가 없거나
// 오프라인이어도 로컬만으로 그대로 동작하고, 연결되면 자동으로 밀어올린다.
// 인증 없는 tailnet 단일 사용자 전제 — 책마다 하나의 공유 상태.

const SYNC_KINDS = ["marks", "favorites", "bookmarks", "recent", "answerSplits", "lastPage"];

function emptyLedger() {
  const l = {};
  for (const k of SYNC_KINDS) l[k] = {};
  return l;
}

let syncLedger = emptyLedger();
let syncStarted = false; // init 완료 전 pull이 렌더 함수를 부르지 않도록 하는 가드
let pushTimer = null;

function loadLedger() {
  let raw = null;
  try {
    raw = JSON.parse(localStorage.getItem(lsKey("sync")));
  } catch {
    raw = null;
  }
  const l = emptyLedger();
  if (raw && typeof raw === "object") {
    for (const k of SYNC_KINDS) {
      if (raw[k] && typeof raw[k] === "object") l[k] = raw[k];
    }
  }
  return l;
}

function persistLedger() {
  localStorage.setItem(lsKey("sync"), JSON.stringify(syncLedger));
}

function syncSet(kind, key, value) {
  syncLedger[kind][String(key)] = { v: value, t: Date.now(), d: 0 };
  persistLedger();
  schedulePush();
}

function syncDelete(kind, key) {
  // tombstone을 남긴다 — 다른 기기엔 아직 살아있을 수 있어 삭제가 전파돼야 한다.
  syncLedger[kind][String(key)] = { v: null, t: Date.now(), d: 1 };
  persistLedger();
  schedulePush();
}

function syncGet(kind, key) {
  const e = syncLedger[kind][String(key)];
  return e && !e.d ? e.v : undefined;
}

function syncLive(kind) {
  return Object.entries(syncLedger[kind]).filter(([, e]) => e && !e.d);
}

// 서버/다른 기기에서 받은 원장을 LWW로 병합한다 (더 최신 t만 반영). 바뀐 게 있으면 true.
function mergeLedger(incoming) {
  let changed = false;
  for (const kind of SYNC_KINDS) {
    const items = incoming && incoming[kind];
    if (!items || typeof items !== "object") continue;
    for (const [key, e] of Object.entries(items)) {
      if (!e || typeof e.t !== "number") continue;
      const cur = syncLedger[kind][key];
      if (!cur || e.t > cur.t) {
        syncLedger[kind][key] = { v: e.d ? null : e.v, t: e.t, d: e.d ? 1 : 0 };
        changed = true;
      }
    }
  }
  if (changed) persistLedger();
  return changed;
}

function schedulePush() {
  if (pushTimer) clearTimeout(pushTimer);
  pushTimer = setTimeout(pushNow, 800);
}

async function pushNow() {
  if (pushTimer) {
    clearTimeout(pushTimer);
    pushTimer = null;
  }
  try {
    const res = await fetch(`${API}/book/${BOOK_ID}/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: syncLedger }),
    });
    if (!res.ok) throw new Error(`${res.status}`);
    const data = await res.json();
    if (mergeLedger(data.state) && syncStarted) applyLedgerToUI();
  } catch (err) {
    console.warn("동기화 push 실패(로컬은 유지):", err.message);
  }
}

async function pullNow() {
  try {
    const data = await fetchJSON(`${API}/book/${BOOK_ID}/state`);
    if (mergeLedger(data.state) && syncStarted) applyLedgerToUI();
  } catch (err) {
    console.warn("동기화 pull 실패(로컬은 유지):", err.message);
  }
}

// 원장 → 화면. 파생 캐시(state.marks/answerSplits)를 다시 만들고 보이는 것 갱신.
function rebuildDerived() {
  state.marks = {};
  for (const [k, e] of syncLive("marks")) state.marks[k] = e.v;
  state.answerSplits = {};
  for (const [k, e] of syncLive("answerSplits")) state.answerSplits[Number(k)] = e.v;
}

function applyLedgerToUI() {
  rebuildDerived();
  renderDrawerLists();
  renderCurrent();
}

// 최초 실행 시 예전 localStorage 키들을 원장으로 이관한다 (원장이 이미 있으면 건너뜀).
// 폰에 이미 쌓인 즐겨찾기/문제표시 등을 잃지 않고 그대로 동기화 대상에 편입한다.
function migrateLegacyIntoLedger() {
  if (localStorage.getItem(lsKey("sync")) != null) return;
  const now = Date.now();
  for (const page of lsGetList("favorites")) {
    syncLedger.favorites[String(page)] = { v: true, t: now, d: 0 };
  }
  for (const b of lsGetList("bookmarks")) {
    if (b && b.page != null) {
      syncLedger.bookmarks[String(b.page)] = { v: { at: b.at || now }, t: b.at || now, d: 0 };
    }
  }
  // recent는 순서(앞이 최신)를 타임스탬프 내림차순으로 보존한다.
  lsGetList("recent").forEach((page, i) => {
    syncLedger.recent[String(page)] = { v: now - i, t: now - i, d: 0 };
  });
  // 문제표시: 예전 배열 형식(["42:2", ...])이면 전부 done으로 마이그레이션.
  let rawMarks = null;
  try {
    rawMarks = JSON.parse(localStorage.getItem(lsKey("solved")));
  } catch {
    rawMarks = null;
  }
  let marksObj = {};
  if (Array.isArray(rawMarks)) {
    for (const k of rawMarks) marksObj[k] = "done";
  } else if (rawMarks && typeof rawMarks === "object") {
    marksObj = rawMarks;
  }
  for (const [k, v] of Object.entries(marksObj)) {
    syncLedger.marks[k] = { v, t: now, d: 0 };
  }
  let splits = {};
  try {
    splits = JSON.parse(localStorage.getItem(lsKey("answerSplits"))) || {};
  } catch {
    splits = {};
  }
  for (const [k, v] of Object.entries(splits)) {
    syncLedger.answerSplits[String(k)] = { v, t: now, d: 0 };
  }
  const lastPage = parseInt(localStorage.getItem(lsKey("lastPage")), 10);
  if (!Number.isNaN(lastPage)) {
    syncLedger.lastPage.value = { v: lastPage, t: now, d: 0 };
  }
  persistLedger();
}

// ---------- 사용자 상태 접근 (원장 위에서) ----------

function isFavorite(page) {
  return syncGet("favorites", page) === true;
}

function toggleFavorite(page) {
  if (isFavorite(page)) syncDelete("favorites", page);
  else syncSet("favorites", page, true);
}

function favoritePages() {
  return syncLive("favorites")
    .map(([k]) => Number(k))
    .sort((a, b) => a - b);
}

// 문제 단위 표시: 미완료(없음) → 완료(done) → 중요(important) 3단계 순환.
// key는 "페이지:문제순번" — 그 페이지 block_order 안에서 problem_number 블록이
// 몇 번째인지(1-indexed)로 잡는다. block.id는 세그멘테이션을 다시 돌리면
// 드리프트하므로(그래서 diff는 IoU로 매칭한다) key로 쓰지 않는다. 순번 방식도 그
// 페이지의 문제 개수/순서가 바뀌면 어긋날 수 있지만 id보다는 훨씬 안정적이다.
function markKey(page, idx) {
  return `${page}:${idx}`;
}

function markOf(page, idx) {
  return state.marks[markKey(page, idx)] || null;
}

function nextMark(cur) {
  if (!cur) return "done";
  if (cur === "done") return "important";
  return null; // 중요 → 미완료
}

function cycleMark(page, idx) {
  const key = markKey(page, idx);
  const next = nextMark(state.marks[key]);
  if (next) {
    state.marks[key] = next;
    syncSet("marks", key, next);
  } else {
    delete state.marks[key];
    syncDelete("marks", key);
  }
  return next;
}

function addBookmark(page) {
  if (syncGet("bookmarks", page) === undefined) {
    syncSet("bookmarks", page, { at: Date.now() });
  }
}

function removeBookmark(page) {
  syncDelete("bookmarks", page);
}

function bookmarkPages() {
  return syncLive("bookmarks")
    .map(([k, e]) => ({ page: Number(k), at: (e.v && e.v.at) || 0 }))
    .sort((a, b) => b.at - a.at)
    .map((b) => b.page);
}

function pushRecent(page) {
  // 방문 시각을 값으로 저장하고, 표시할 땐 시각 내림차순으로 정렬해 최근 20개만 쓴다.
  syncSet("recent", page, Date.now());
}

function recentPages() {
  return syncLive("recent")
    .sort((a, b) => (b[1].v || 0) - (a[1].v || 0))
    .slice(0, 20)
    .map(([k]) => Number(k));
}

// ---------- API ----------

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

function pageImageUrl(page) {
  return `${API}/book/${BOOK_ID}/page/${page}`;
}

function answerImageUrl(page) {
  return `${API}/book/${BOOK_ID}/answer/${page}`;
}

// 현재 교재 페이지가 어느 답지 페이지에 대응하는지 (answers.json의 page_map).
// 범위 밖이면 가장 가까운 끝값으로 clamp.
function answerPageFor(bookPage) {
  const map = state.answers.page_map;
  if (map[bookPage] != null) return map[bookPage];
  const keys = Object.keys(map).map(Number);
  const lo = Math.min(...keys), hi = Math.max(...keys);
  return map[Math.max(lo, Math.min(hi, bookPage))];
}

// ---------- DOM refs ----------

const el = {
  pageIndicator: document.getElementById("page-indicator"),
  btnFavorite: document.getElementById("btn-favorite"),
  btnBookmark: document.getElementById("btn-bookmark"),
  modeImage: document.getElementById("mode-image"),
  modeReflow: document.getElementById("mode-reflow"),
  pageView: document.getElementById("page-view"),
  pageImage: document.getElementById("page-image"),
  pageEmptyHint: document.getElementById("page-empty-hint"),
  reflowView: document.getElementById("reflow-view"),
  btnPrev: document.getElementById("btn-prev"),
  btnNext: document.getElementById("btn-next"),
  pageInput: document.getElementById("page-input"),
  pageTotal: document.getElementById("page-total"),
  btnMenu: document.getElementById("btn-menu"),
  btnCloseDrawer: document.getElementById("btn-close-drawer"),
  drawer: document.getElementById("drawer"),
  unitSelect: document.getElementById("unit-select"),
  listFavorites: document.getElementById("list-favorites"),
  listBookmarks: document.getElementById("list-bookmarks"),
  listRecent: document.getElementById("list-recent"),
  content: document.getElementById("content"),
  btnAnswer: document.getElementById("btn-answer"),
  answerModal: document.getElementById("answer-modal"),
  answerBody: document.getElementById("answer-body"),
  answerCanvas: document.getElementById("answer-canvas"),
  answerLabel: document.getElementById("answer-label"),
  answerPrev: document.getElementById("answer-prev"),
  answerNext: document.getElementById("answer-next"),
  answerMode: document.getElementById("answer-mode"),
  answerClose: document.getElementById("answer-close"),
  answerReveal: document.getElementById("answer-reveal"),
  answerAdjust: document.getElementById("answer-adjust"),
  answerSplit: document.getElementById("answer-split"),
  answerSplitReset: document.getElementById("answer-split-reset"),
};

// ---------- 렌더링 ----------

function renderPageImage(page) {
  el.pageEmptyHint.hidden = true;
  el.pageImage.hidden = false;
  el.pageImage.src = pageImageUrl(page);
  el.pageImage.onerror = () => {
    el.pageImage.hidden = true;
    el.pageEmptyHint.hidden = false;
  };
}

function renderReflow(page) {
  const meta = state.pagesByNumber.get(page);
  el.reflowView.innerHTML = "";

  if (!meta || !meta.block_order || meta.block_order.length === 0) {
    const p = document.createElement("p");
    p.className = "empty-hint";
    p.textContent = "이 페이지는 아직 블록 분석이 없습니다. 원본 보기를 이용하세요.";
    el.reflowView.appendChild(p);
    return;
  }

  // clientWidth는 padding까지 포함한 값이라, 그걸 그대로 블록 폭으로 쓰면
  // 좌우 padding(28px)만큼 넘쳐서 가로 스크롤이 생긴다 — 실제 콘텐츠 폭만 써야 한다.
  const reflowStyle = getComputedStyle(el.reflowView);
  const horizontalPadding = parseFloat(reflowStyle.paddingLeft) + parseFloat(reflowStyle.paddingRight);
  const containerW = (el.reflowView.clientWidth || 320) - horizontalPadding;
  const imgUrl = pageImageUrl(page);
  const pageW = meta.width_px;
  const pageH = meta.height_px;

  let problemIdx = 0;
  for (const blockId of meta.block_order) {
    const block = state.blocksById.get(blockId);
    if (!block) continue;
    const isProblem = block.type === "problem_number";
    if (isProblem) problemIdx += 1;
    const [x, y, w, h] = block.bbox;
    const role = (block.reflow && block.reflow.role) || "paragraph";
    const layout = ROLE_LAYOUT[role] || ROLE_LAYOUT.paragraph;
    const maxScale = role === "paragraph" ? TEXT_MAX_SCALE : MAX_SCALE;

    // 줄이 1개뿐이어도(제목 옆 장식 여백을 실제 잉크 범위로 좁힌 경우 등) 그
    // 폭을 배율 계산에 써야 한다 — length>1로만 걸면 이런 1줄짜리는 block.bbox
    // (아직 장식 여백 포함된 넓은 원래 폭)로 다시 새서 배율이 안 커진다.
    const hasLines = (role === "paragraph" || role === "equation") && Array.isArray(block.lines) && block.lines.length > 0;

    let scale;
    if (layout.mode === "auto") {
      scale = layout.targetHeightPx / (h * pageH);
    } else if (hasLines) {
      // 줄 단위로 쪼갠 경우, 원래 블록 전체 폭(w)이 아니라 그 중 가장 넓은
      // 개별 줄의 폭을 기준으로 배율을 정해야 한다 — 안 그러면 강제 줄바꿈으로
      // 줄을 좁혀놔도 화면에는 여전히 옛날(안 쪼갠) 폭 기준 작은 배율 그대로
      // 그려져서 글자가 안 커진다.
      const maxLineW = Math.max(...block.lines.map((ln) => ln.bbox[2]));
      scale = containerW / (maxLineW * pageW);
    } else {
      scale = containerW / (w * pageW);
    }
    // 원본 해상도 이상으로 늘리면 흐려진다 — 폭이 좁은 블록을 화면 폭까지
    // 억지로 키우지 않도록 배율에 상한을 둔다(글자는 그림보다 상한이 느슨함).
    // 상한에 걸리면 화면 폭을 다 못 채우니 가운데 정렬한다.
    scale = Math.min(scale, maxScale);

    if (hasLines) {
      // figure/table과 달리 text/formula는 줄 사이가 뚜렷이 비어 있으면(문단의
      // 줄바꿈, 여러 줄 수식 유도 과정) 줄마다 독립적이다 — 편집기가 분수·지수처럼
      // 한 줄 안에서 2차원 구조인 부분은 애초에 안 쪼개고 lines를 만들어 보낸다.
      // 문단/수식 전체를 이미지 한 장으로 뭉치지 않고 줄 단위로 잘라 쌓으면, 짧은
      // 마지막 줄이 억지로 늘어나 보이지 않고 좁은 블록도 상한 안에서 최대한 커진다.
      const group = document.createElement("div");
      group.className = `rblock-group role-${role}`;
      for (const line of block.lines) {
        const lineDiv = cropDiv(imgUrl, pageW, pageH, line.bbox, scale, `role-${role}`);
        lineDiv.classList.add("rline");
        group.appendChild(lineDiv);
      }
      el.reflowView.appendChild(group);
      continue;
    }

    const div = cropDiv(imgUrl, pageW, pageH, block.bbox, scale, `role-${role}`);
    const fullW = pageW * scale;
    if (layout.mode === "full" && w * fullW < containerW) {
      div.style.margin = "0 auto 22px"; // 상한에 걸려 폭을 못 채운 경우 가운데 정렬
    }

    // 문제 번호에는 옆에 "풀었음" 토글을 붙여 한 줄로 감싼다.
    if (isProblem) {
      el.reflowView.appendChild(problemRow(div, page, problemIdx));
    } else {
      el.reflowView.appendChild(div);
    }
  }
}

/** problem_number 크롭 옆에 3단계(미완료/완료/중요) 표시 토글을 붙인 한 줄. */
function problemRow(cropEl, page, idx) {
  const row = document.createElement("div");
  row.className = "problem-row";
  cropEl.style.margin = "0"; // 간격은 row가 관리한다

  const btn = document.createElement("button");
  btn.className = "solved-toggle";
  btn.type = "button";
  btn.title = "탭할 때마다: 미완료 → 완료 → 중요";

  const apply = (mark) => {
    row.classList.toggle("done", mark === "done");
    row.classList.toggle("important", mark === "important");
    btn.setAttribute("data-mark", mark || "");
    btn.textContent = mark === "done" ? "✓" : mark === "important" ? "★" : "";
    btn.setAttribute("aria-label", mark === "done" ? "완료" : mark === "important" ? "중요" : "미완료");
  };
  apply(markOf(page, idx));

  btn.onclick = () => apply(cycleMark(page, idx));

  row.appendChild(cropEl);
  row.appendChild(btn);
  return row;
}

/** 페이지 이미지에서 bbox(정규화 좌표) 영역만 CSS 배경으로 잘라낸 div를 만든다. */
function cropDiv(imgUrl, pageW, pageH, bbox, scale, extraClass) {
  const [x, y, w, h] = bbox;
  const fullW = pageW * scale;
  const fullH = pageH * scale;
  const div = document.createElement("div");
  div.className = `rblock ${extraClass}`;
  div.style.width = `${w * fullW}px`;
  div.style.height = `${h * fullH}px`;
  div.style.backgroundImage = `url(${imgUrl})`;
  div.style.backgroundSize = `${fullW}px ${fullH}px`;
  div.style.backgroundPosition = `${-x * fullW}px ${-y * fullH}px`;
  return div;
}

function renderCurrent() {
  const page = state.currentPage;
  const unitShort = state.unit.title.split(".")[0]; // "Ⅰ-1. 평면좌표" -> "Ⅰ-1"
  el.pageIndicator.textContent = `${page}쪽 · ${unitShort}`;
  el.pageInput.value = page;
  el.btnFavorite.textContent = isFavorite(page) ? "★" : "☆";
  el.btnFavorite.classList.toggle("on", isFavorite(page));
  el.btnPrev.disabled = page <= state.unit.start;
  el.btnNext.disabled = page >= state.unit.end;
  el.btnAnswer.hidden = !state.answers; // 답지가 있는 책에서만 노출

  if (state.viewMode === "image") {
    renderPageImage(page);
  } else {
    renderReflow(page);
  }
}

function setViewMode(mode) {
  state.viewMode = mode;
  el.modeImage.classList.toggle("active", mode === "image");
  el.modeReflow.classList.toggle("active", mode === "reflow");
  el.pageView.hidden = mode !== "image";
  el.reflowView.hidden = mode !== "reflow";
  renderCurrent();
}

function goToPage(page) {
  page = Math.max(state.unit.start, Math.min(state.unit.end, page));
  state.currentPage = page;
  pushRecent(page);
  renderCurrent();
  renderDrawerLists();
  syncSet("lastPage", "value", page);
  el.content.scrollTop = 0;
}

function applyUnit(unit) {
  state.unit = unit;
  el.pageInput.min = unit.start;
  el.pageInput.max = unit.end;
  el.pageTotal.textContent = `(${unit.start}~${unit.end})`;
  el.unitSelect.value = unit.id;
}

function openUnit(unit) {
  applyUnit(unit);
  goToPage(unit.start);
}

// 북마크·즐겨찾기·최근 목록은 다른 단원의 페이지를 가리킬 수 있다 — goToPage는
// 현재 단원 범위로 clamp하므로, 필요하면 먼저 그 페이지가 속한 단원으로 바꾼다.
function goToPageAnyUnit(page) {
  const unit = unitForPage(page);
  if (unit.id !== state.unit.id) applyUnit(unit);
  goToPage(page);
}

function renderDrawerLists() {
  renderList(el.listFavorites, favoritePages(), (page) => `${page}쪽`, true);
  renderList(el.listBookmarks, bookmarkPages(), (page) => `${page}쪽`, false, removeBookmark);
  renderList(el.listRecent, recentPages(), (page) => `${page}쪽`, false);
}

function renderList(ulEl, pages, label, removeIsFavoriteToggle, onRemove) {
  ulEl.innerHTML = "";
  if (pages.length === 0) {
    const li = document.createElement("li");
    li.className = "drawer-empty";
    li.textContent = "없음";
    ulEl.appendChild(li);
    return;
  }
  for (const page of pages) {
    const li = document.createElement("li");
    const jump = document.createElement("button");
    jump.className = "jump";
    jump.textContent = label(page);
    jump.onclick = () => {
      goToPageAnyUnit(page);
      closeDrawer();
    };
    li.appendChild(jump);

    if (removeIsFavoriteToggle) {
      const rm = document.createElement("button");
      rm.className = "remove";
      rm.textContent = "✕";
      rm.onclick = () => {
        toggleFavorite(page);
        renderDrawerLists();
        renderCurrent();
      };
      li.appendChild(rm);
    } else if (onRemove) {
      const rm = document.createElement("button");
      rm.className = "remove";
      rm.textContent = "✕";
      rm.onclick = () => {
        onRemove(page);
        renderDrawerLists();
      };
      li.appendChild(rm);
    }
    ulEl.appendChild(li);
  }
}

function openDrawer() {
  el.drawer.hidden = false;
}
function closeDrawer() {
  el.drawer.hidden = true;
}

// ---------- 답지 모달 ----------

// 답지 페이지 이미지에서 한 열(정규화 x0~x1)만 잘라 컨테이너 폭에 맞춰 채운 div.
// 답지가 2단 조판이라, 좌측단·우측단을 각각 이렇게 잘라 세로로 쌓으면 폰에서
// 한 줄기로 읽힌다 (리플로우의 CSS 배경 크롭과 같은 기법, 새 이미지 파일 불필요).
function answerColumn(url, x0, x1, aspect, containerW) {
  const wn = x1 - x0;
  const fullW = containerW / wn; // 페이지 전체가 이 배율로 그려질 때의 폭
  const fullH = fullW * aspect;
  const div = document.createElement("div");
  div.className = "answer-col";
  div.style.width = `${containerW}px`;
  div.style.height = `${fullH}px`; // 열은 페이지 전체 높이
  div.style.backgroundImage = `url(${url})`;
  div.style.backgroundSize = `${fullW}px ${fullH}px`;
  div.style.backgroundPosition = `${-x0 * fullW}px 0px`;
  return div;
}

// 2단 분할선(거터) 위치. 답지가 스캔본이라 홀수(오른쪽)/짝수(왼쪽) 페이지마다
// 거터 x가 다르고(측정: 0.462 vs 0.490) 페이지별 편차도 있어, 파리티 기본값을
// 두되 페이지별로 슬라이더로 조정·저장(localStorage)할 수 있게 한다.
const ANSWER_GUTTER_ODD = 0.462;
const ANSWER_GUTTER_EVEN = 0.49;
const ANSWER_MARGIN_L = 0.05;
const ANSWER_MARGIN_R = 0.98;

function answerGutter(page) {
  const ov = state.answerSplits[page];
  if (ov != null) return ov;
  return page % 2 === 1 ? ANSWER_GUTTER_ODD : ANSWER_GUTTER_EVEN;
}

// 캔버스에 열 크롭을 그린다 (스크롤 위치는 건드리지 않음 — 슬라이더 조정 중에도 유지).
function layoutAnswer() {
  const n = state.answerPage;
  const url = answerImageUrl(n);
  const containerW = el.answerBody.clientWidth || 360;
  const aspect = state.answers.page_h / state.answers.page_w;
  el.answerCanvas.innerHTML = "";
  if (state.answerMode === "split") {
    const g = answerGutter(n);
    el.answerCanvas.appendChild(answerColumn(url, ANSWER_MARGIN_L, g, aspect, containerW));
    el.answerCanvas.appendChild(answerColumn(url, g, ANSWER_MARGIN_R, aspect, containerW));
  } else {
    el.answerCanvas.appendChild(answerColumn(url, 0, 1, aspect, containerW));
  }
}

function renderAnswer() {
  const n = state.answerPage;
  el.answerLabel.textContent = `답지 ${n} / ${state.answers.count}쪽`;
  el.answerPrev.disabled = n <= 1;
  el.answerNext.disabled = n >= state.answers.count;
  el.answerMode.textContent = state.answerMode === "split" ? "전체" : "2단";
  el.answerAdjust.hidden = state.answerMode !== "split"; // 분할선 조정 바는 2단에서만
  el.answerSplit.value = String(answerGutter(n));
  layoutAnswer();
  el.answerBody.scrollTop = 0;
}

function openAnswer() {
  if (!state.answers) return;
  state.answerPage = answerPageFor(state.currentPage);
  el.answerBody.classList.add("spoiled"); // 열 때는 항상 가려진 상태로 시작
  el.answerModal.hidden = false; // 먼저 보이게 해야 clientWidth가 잡힌다
  renderAnswer();
}

function closeAnswer() {
  el.answerModal.hidden = true;
}

// 이전/다음 답지쪽으로 이동 — 한 번 답을 봤으면(spoiled 해제) 그대로 유지한다.
function stepAnswer(delta) {
  const next = Math.max(1, Math.min(state.answers.count, state.answerPage + delta));
  if (next === state.answerPage) return;
  state.answerPage = next;
  renderAnswer();
}

// ---------- 이벤트 ----------

el.modeImage.onclick = () => setViewMode("image");
el.modeReflow.onclick = () => setViewMode("reflow");
el.btnPrev.onclick = () => goToPage(state.currentPage - 1);
el.btnNext.onclick = () => goToPage(state.currentPage + 1);

// 좌우 화살표로 페이지 이동, 위아래 화살표로 스크롤(페이지 업/다운 방식).
// 페이지 입력창 등에 포커스가 있을 때는 원래 하던 대로 커서 이동에 쓰이게
// 두고 가로채지 않는다.
window.addEventListener("keydown", (e) => {
  // 답지 모달이 열려 있으면 좌우 화살표는 항상 답지쪽 이동만 한다. INPUT 가드보다
  // 먼저 처리하고 preventDefault해서, 포커스가 분할선 슬라이더(range)에 있어도
  // 화살표가 슬라이더를 움직이지 않게 한다 (슬라이더는 드래그로만 조정). Esc는 닫기.
  if (!el.answerModal.hidden) {
    if (e.key === "Escape") { e.preventDefault(); closeAnswer(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); stepAnswer(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); stepAnswer(1); }
    return;
  }

  const tag = document.activeElement && document.activeElement.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault();
    goToPage(state.currentPage + (e.key === "ArrowRight" ? 1 : -1));
  } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    e.preventDefault();
    const amount = el.content.clientHeight * 0.9;
    el.content.scrollBy({ top: e.key === "ArrowDown" ? amount : -amount, behavior: "smooth" });
  }
});
el.pageInput.onchange = () => goToPage(parseInt(el.pageInput.value, 10) || state.currentPage);
el.btnFavorite.onclick = () => {
  toggleFavorite(state.currentPage);
  renderCurrent();
  renderDrawerLists();
};
el.btnBookmark.onclick = () => {
  addBookmark(state.currentPage);
  renderDrawerLists();
  openDrawer();
};
el.btnMenu.onclick = openDrawer;
el.btnCloseDrawer.onclick = closeDrawer;
el.drawer.querySelector(".drawer-backdrop").onclick = closeDrawer;

el.btnAnswer.onclick = openAnswer;
el.answerClose.onclick = closeAnswer;
el.answerModal.querySelector(".answer-backdrop").onclick = closeAnswer;
el.answerReveal.onclick = () => el.answerBody.classList.remove("spoiled");
el.answerPrev.onclick = () => stepAnswer(-1);
el.answerNext.onclick = () => stepAnswer(1);
el.answerMode.onclick = () => {
  state.answerMode = state.answerMode === "split" ? "full" : "split";
  renderAnswer();
};
// 분할선 조정: 드래그 중엔 즉시 재배치(스크롤 유지, 원장은 안 건드림), 놓을 때 원장에 저장.
el.answerSplit.oninput = () => {
  state.answerSplits[state.answerPage] = parseFloat(el.answerSplit.value);
  layoutAnswer();
};
el.answerSplit.onchange = () => {
  syncSet("answerSplits", state.answerPage, state.answerSplits[state.answerPage]);
};
el.answerSplitReset.onclick = () => {
  delete state.answerSplits[state.answerPage]; // 파리티 기본값으로 되돌림
  syncDelete("answerSplits", state.answerPage);
  renderAnswer();
};
el.unitSelect.onchange = () => {
  const unit = UNITS.find((u) => u.id === el.unitSelect.value);
  if (unit) {
    openUnit(unit);
    closeDrawer();
  }
};

// ---------- 초기화 ----------

async function init() {
  const [book, pages, blocks] = await Promise.all([
    fetchJSON(`${API}/book/${BOOK_ID}`),
    fetchJSON(`${API}/book/${BOOK_ID}/pages`),
    fetchJSON(`${API}/book/${BOOK_ID}/blocks`),
  ]);

  state.book = book;
  for (const p of pages.pages) state.pagesByNumber.set(p.number, p);
  for (const b of blocks.blocks) state.blocksById.set(b.id, b);

  // 답지는 선택 사항 — 없는 책이면 404라 답지 기능만 조용히 꺼진다.
  try {
    state.answers = await fetchJSON(`${API}/book/${BOOK_ID}/answers`);
  } catch {
    state.answers = null;
  }

  // 동기화 원장 로드(없으면 예전 localStorage 키에서 이관) → 파생 캐시 재구성.
  syncLedger = loadLedger();
  migrateLegacyIntoLedger();
  rebuildDerived();

  for (const unit of UNITS) {
    const opt = document.createElement("option");
    opt.value = unit.id;
    opt.textContent = `${unit.title} (${unit.start}~${unit.end})`;
    el.unitSelect.appendChild(opt);
  }

  // 마지막으로 보던 페이지가 있으면 그 페이지가 속한 단원을 열고, 없으면 첫 단원부터.
  const saved = Number(syncGet("lastPage", "value")) || 0;
  const startUnit = saved ? unitForPage(saved) : UNITS[0];
  const startPage = saved && saved >= startUnit.start && saved <= startUnit.end ? saved : startUnit.start;

  applyUnit(startUnit);
  renderDrawerLists();
  goToPage(startPage);

  // 이제부터 pull이 화면을 갱신해도 안전하다. 서버에서 받아 병합(pull)한 뒤,
  // 로컬에만 있던 최신 항목을 밀어올린다(push) — 양방향 화해.
  syncStarted = true;
  pullNow().then(pushNow);
}

init().catch((err) => {
  document.body.innerHTML = `<p style="padding:20px;color:#c00;">불러오기 실패: ${err.message}</p>`;
  console.error(err);
});

// 화면 회전/리사이즈 시 다시 계산 (폭 기반 배율이라 필수)
window.addEventListener("resize", () => {
  if (!el.answerModal.hidden) renderAnswer();
  else if (state.viewMode === "reflow") renderReflow(state.currentPage);
});

// 다른 기기에서 바꾼 걸 반영하려고, 창이 다시 보이거나 포커스될 때 서버에서 pull한다
// (폴링은 안 함 — 다시 볼 때만 가볍게 당겨온다). 짧은 디바운스로 중복 호출을 줄인다.
let focusPullTimer = null;
function scheduleFocusPull() {
  if (!syncStarted) return;
  if (focusPullTimer) clearTimeout(focusPullTimer);
  focusPullTimer = setTimeout(() => {
    focusPullTimer = null;
    pullNow();
  }, 300);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") scheduleFocusPull();
});
window.addEventListener("focus", scheduleFocusPull);
