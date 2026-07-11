// MathFlow 웹 뷰어 — 빌드 없는 순수 JS. 서버는 book/pages/blocks.json과 페이지
// 이미지를 읽기 전용으로 서빙만 하고, 북마크/즐겨찾기/최근 페이지는 아직 서버
// DB가 없어서 이 브라우저(localStorage)에만 저장된다 — 기기 간 동기화는 안 됨.

const BOOK_ID = "gongtong-math-2";
const API = ""; // 같은 오리진에서 서빙되므로 상대경로

const UNITS = [
  { id: "I-1", title: "Ⅰ-1. 평면좌표", start: 10, end: 32 },
  { id: "I-2", title: "Ⅰ-2. 직선의 방정식", start: 33, end: 65 },
  { id: "I-3", title: "Ⅰ-3. 원의 방정식", start: 66, end: 97 },
  { id: "I-4", title: "Ⅰ-4. 도형의 이동", start: 98, end: 119 },
  { id: "II-1", title: "Ⅱ-1. 집합의 뜻과 포함 관계", start: 120, end: 137 },
  { id: "II-2", title: "Ⅱ-2. 집합의 연산", start: 138, end: 165 },
  { id: "II-3", title: "Ⅱ-3. 명제", start: 166, end: 205 },
  { id: "III-1", title: "Ⅲ-1. 함수", start: 206, end: 247 },
  { id: "III-2", title: "Ⅲ-2. 유리함수", start: 248, end: 275 },
  { id: "III-3", title: "Ⅲ-3. 무리함수", start: 276, end: 291 },
];

const ROLE_LAYOUT = {
  label: { mode: "auto", targetHeightPx: 26 },
  paragraph: { mode: "full" },
  equation: { mode: "full" },
  figure: { mode: "full" },
  table: { mode: "full" },
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
};

function unitForPage(page) {
  return UNITS.find((u) => page >= u.start && page <= u.end) || UNITS[0];
}

// ---------- localStorage (책 단위로 네임스페이스) ----------

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

function lsSetList(kind, list) {
  localStorage.setItem(lsKey(kind), JSON.stringify(list));
}

function isFavorite(page) {
  return lsGetList("favorites").includes(page);
}

function toggleFavorite(page) {
  const list = lsGetList("favorites");
  const i = list.indexOf(page);
  if (i >= 0) list.splice(i, 1);
  else list.push(page);
  lsSetList("favorites", list);
}

function addBookmark(page) {
  const list = lsGetList("bookmarks");
  if (!list.some((b) => b.page === page)) {
    list.unshift({ page, at: Date.now() });
    lsSetList("bookmarks", list);
  }
}

function removeBookmark(page) {
  lsSetList("bookmarks", lsGetList("bookmarks").filter((b) => b.page !== page));
}

function pushRecent(page) {
  let list = lsGetList("recent").filter((p) => p !== page);
  list.unshift(page);
  list = list.slice(0, 20);
  lsSetList("recent", list);
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

  for (const blockId of meta.block_order) {
    const block = state.blocksById.get(blockId);
    if (!block) continue;
    const [x, y, w, h] = block.bbox;
    const role = (block.reflow && block.reflow.role) || "paragraph";
    const layout = ROLE_LAYOUT[role] || ROLE_LAYOUT.paragraph;
    const maxScale = role === "paragraph" ? TEXT_MAX_SCALE : MAX_SCALE;

    const hasLines = (role === "paragraph" || role === "equation") && Array.isArray(block.lines) && block.lines.length > 1;

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
    el.reflowView.appendChild(div);
  }
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
  localStorage.setItem(lsKey("lastPage"), String(page));
  document.getElementById("content").scrollTop = 0;
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
  renderList(el.listFavorites, lsGetList("favorites").sort((a, b) => a - b), (page) => `${page}쪽`, true);
  renderList(
    el.listBookmarks,
    lsGetList("bookmarks").map((b) => b.page),
    (page) => `${page}쪽`,
    false,
    removeBookmark
  );
  renderList(el.listRecent, lsGetList("recent"), (page) => `${page}쪽`, false);
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

// ---------- 이벤트 ----------

el.modeImage.onclick = () => setViewMode("image");
el.modeReflow.onclick = () => setViewMode("reflow");
el.btnPrev.onclick = () => goToPage(state.currentPage - 1);
el.btnNext.onclick = () => goToPage(state.currentPage + 1);

// 좌우 화살표로 페이지 이동. 페이지 입력창 등에 포커스가 있을 때는 원래
// 하던 대로 커서 이동에 쓰이게 두고 페이지 넘김으로 가로채지 않는다.
window.addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const tag = document.activeElement && document.activeElement.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
  e.preventDefault();
  goToPage(state.currentPage + (e.key === "ArrowRight" ? 1 : -1));
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

  for (const unit of UNITS) {
    const opt = document.createElement("option");
    opt.value = unit.id;
    opt.textContent = `${unit.title} (${unit.start}~${unit.end})`;
    el.unitSelect.appendChild(opt);
  }

  // 마지막으로 보던 페이지가 있으면 그 페이지가 속한 단원을 열고, 없으면 첫 단원부터.
  const saved = parseInt(localStorage.getItem(lsKey("lastPage")), 10);
  const startUnit = saved ? unitForPage(saved) : UNITS[0];
  const startPage = saved && saved >= startUnit.start && saved <= startUnit.end ? saved : startUnit.start;

  applyUnit(startUnit);
  renderDrawerLists();
  goToPage(startPage);
}

init().catch((err) => {
  document.body.innerHTML = `<p style="padding:20px;color:#c00;">불러오기 실패: ${err.message}</p>`;
  console.error(err);
});

// 리플로우 모드에서 화면 회전/리사이즈 시 다시 계산
window.addEventListener("resize", () => {
  if (state.viewMode === "reflow") renderReflow(state.currentPage);
});
