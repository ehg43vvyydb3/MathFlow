"""블록 검토/보정 화면.

segment+VLM 파이프라인이 만든 블록을 페이지 이미지 위에 색상으로 겹쳐 보여주고,
타입 수정/병합/삭제/새 블록 추가만 지원한다 (리사이즈 드래그 핸들은 v1에서 생략 —
필요하면 삭제 후 새로 그리는 것으로 대체). "다음 검토 필요 페이지"는 VLM confidence가
아니라 같은 페이지·같은 타입 블록 대비 면적이 비정상적으로 큰 블록(병합 실패 의심,
review.py)을 기준으로 삼는다 — VLM 자체 확신도는 실측상 신호가 안 됐기 때문이다.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QImage, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import units as units_module
from ..analysis import pipeline, review, segment
from ..analysis.vlm_client import BLOCK_TYPES, OllamaBackend
from ..io import export, metadata
from .flow_layout import FlowLayout

REPO_ROOT = Path(__file__).resolve().parents[3]

BOOK_TITLE = "공통수학2"
BOOK_PAGE_COUNT = 304

_TYPE_QCOLOR = {
    "text": QColor(230, 140, 0),
    "figure": QColor(0, 150, 0),
    "formula": QColor(30, 30, 220),
    "table": QColor(170, 0, 170),
    "problem_number": QColor(0, 150, 150),
    "page_number": QColor(140, 90, 40),
}

_TYPE_LABEL_KO = {
    "text": "텍스트",
    "figure": "그림·그래프",
    "formula": "수식",
    "table": "표",
    "problem_number": "문제번호",
    "page_number": "쪽번호",
}

# 숫자키로 선택 블록 타입을 바로 바꿀 수 있게 각 타입에 1부터 번호를 매긴다.
# _TYPE_QCOLOR(따라서 범례에 표시되는 순서)와 항상 같은 순서를 유지해야 한다 —
# 범례에 적힌 숫자와 실제로 눌러야 할 키가 어긋나면 안 되기 때문.
_TYPE_SHORTCUT_KEYS = {t: str(i) for i, t in enumerate(_TYPE_QCOLOR, start=1) if i <= 9}

# 이 책의 페이지는 본문(왼쪽 ~70%)과 사이드바(오른쪽)로 나뉜다 — 읽는 순서는
# 컬럼별로 먼저 묶고 그 안에서 위에서 아래로. x=0.5를 기준으로 삼는 건 대략적인
# 근사치지만, 다른 정교한 컬럼 검출 없이도 "병합/새 블록이 리스트 끝에 붙어서
# 리플로우 순서가 화면 위치와 어긋나는" 문제는 이걸로 충분히 해결된다.
_COLUMN_SPLIT_X = 0.5

# problem_number와 그 옆 본문은 y0가 거의 같게(1px 미만 차이) 잡히는 경우가
# 흔한데, 순수 y 비교만 하면 이 미세한 오차 때문에 번호가 자기 내용보다 뒤로
# 밀리는 경우가 실제로 있었다(11쪽에서 확인: 문제10의 본문 y0=0.8825가
# 문제10의 번호 y0=0.8829보다 작아서 번호가 밀림 — 차이는 0.0004뿐).
# 같은 "행"으로 볼 수 있을 만큼 y가 가까우면(반 줄 높이 이내) problem_number를
# 항상 먼저 오게 한다.
_Y_ROW_TOLERANCE = 0.008


def _reading_order_key(block: dict) -> tuple[int, int, int, int]:
    x, y, _w, _h = block["bbox"]
    # page_number(쪽번호)는 컬럼·y위치와 무관하게 항상 그 페이지의 맨 마지막에
    # 온다 — 실제 책에서도 쪽번호는 본문/사이드바 내용과 상관없이 페이지
    # 가장자리에 따로 찍혀 있으므로, 리플로우에서도 "이 페이지 콘텐츠 다음"
    # 이라는 의미로 항상 끝에 배치한다.
    is_page_number = 1 if block["type"] == "page_number" else 0
    column = 1 if x > _COLUMN_SPLIT_X else 0
    y_bucket = round(y / _Y_ROW_TOLERANCE)
    type_priority = 0 if block["type"] == "problem_number" else 1
    return (is_page_number, column, y_bucket, type_priority)


# 위치 연결(attach)에서 "옮길 수 있는" 소스 블록 타입. problem_number는 문제 경계라
# 제자리에 둔다. 대상(어디 뒤에 붙일지)은 자기 자신만 아니면 어떤 블록이든 된다.
_ATTACH_SOURCE_TYPES = {"text", "figure", "formula", "table"}


def _block_center(block: dict, page_w: int, page_h: int) -> tuple[float, float]:
    x, y, w, h = block["bbox"]
    return ((x + w / 2) * page_w, (y + h / 2) * page_h)


def _attach_target(entry: dict) -> str | None:
    return (entry["block"].get("reflow") or {}).get("attach_to")


def _apply_attachments(entries: list[dict]) -> list[dict]:
    """reflow.attach_to가 지정된 블록(주로 figure)을 대상 블록 바로 뒤로 옮긴다.

    그림이 문제와 멀리 떨어져 위치 기준 정렬로는 엉뚱한 데 끼는 경우, 사용자가
    편집기에서 대상 문제를 직접 지정해 그 뒤에 붙이게 하는 기능. 대상이 같은
    페이지에 없으면(삭제됐거나 대상도 attach된 경우) 그림을 맨 뒤에 그대로 둔다.
    입력 entries는 이미 _reading_order_key로 정렬돼 있다고 가정한다.
    """
    if not any(_attach_target(e) for e in entries):
        return entries
    remaining = [e for e in entries if not _attach_target(e)]
    after: dict[str, list[dict]] = {}
    for e in entries:
        tid = _attach_target(e)
        if tid:
            after.setdefault(tid, []).append(e)
    result: list[dict] = []
    placed: set[int] = set()
    for e in remaining:
        result.append(e)
        for a in after.get(e["block"]["id"], []):
            result.append(a)
            placed.add(id(a))
    for e in entries:  # 대상을 못 찾은 attach 블록은 맨 뒤에 붙인다
        if _attach_target(e) and id(e) not in placed:
            result.append(e)
    return result


def _build_legend_html() -> str:
    rows = []
    for t, color in _TYPE_QCOLOR.items():
        # 어두운 범례 배경에서도 잘 보이도록 견본색만 살짝 밝힌다 (블록 테두리 원색 유지).
        swatch = color.lighter(140).name()
        key = _TYPE_SHORTCUT_KEYS.get(t, "")
        rows.append(
            f'<tr><td style="color:#aaaaaa;font-size:12px;">{key}</td>'
            f'<td style="color:{swatch};font-size:15px;padding-left:4px;">■</td>'
            f'<td style="padding-left:4px;">{_TYPE_LABEL_KO[t]}</td></tr>'
        )
    rows.append(
        '<tr><td></td><td style="font-size:13px;">┄</td>'
        '<td style="padding-left:4px;">검토 필요 (점선)</td></tr>'
    )
    return f'<table style="margin:2px;">{"".join(rows)}</table>'


def _format_eta(seconds: float) -> str:
    """남은 예상 시간을 "n분 m초"/"n초"로 표시한다."""
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _cv_to_qpixmap(img: np.ndarray) -> QPixmap:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


class BlockItem(QGraphicsRectItem):
    EDGE_GRAB_PX = 8  # 가장자리에서 이 거리(scene px) 안이면 리사이즈 드래그로 취급

    def __init__(self, block: dict, needs_review: bool, page_w: int, page_h: int):
        x, y, w, h = block["bbox"]
        super().__init__(x * page_w, y * page_h, w * page_w, h * page_h)
        self.block = block
        self.needs_review = needs_review
        self.page_w = page_w
        self.page_h = page_h
        self._resize_edges: str = ""  # 예: "L", "TR", "B"
        self.on_dirty = None  # 리사이즈로 bbox가 바뀔 때 호출할 콜백 (ReviewWindow가 채워줌)
        # 블록 연결(지정 모드 진입/해제) 콜백 — ReviewWindow가 채워준다.
        self.on_request_attach = None
        self.on_request_detach = None
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.apply_style()

    def contextMenuEvent(self, event) -> None:
        """옮길 수 있는 블록을 우클릭하면 '블록 연결'(지정 모드 진입)/'연결 해제' 메뉴."""
        if self.block["type"] not in _ATTACH_SOURCE_TYPES:
            return
        menu = QMenu()
        act_attach = menu.addAction("블록 연결")
        act_detach = None
        if (self.block.get("reflow") or {}).get("attach_to"):
            act_detach = menu.addAction("연결 해제")
        chosen = menu.exec(event.screenPos())
        if chosen is act_attach and self.on_request_attach:
            self.on_request_attach(self)
        elif act_detach is not None and chosen is act_detach and self.on_request_detach:
            self.on_request_detach(self)

    def apply_style(self) -> None:
        color = _TYPE_QCOLOR.get(self.block["type"], QColor(120, 120, 120))
        pen = QPen(color, 2)
        if self.needs_review:
            pen.setStyle(Qt.PenStyle.DashLine)
        # "블록 연결"로 대상에 묶인 블록은 굵은 점선-대시로 눈에 띄게 표시.
        if (self.block.get("reflow") or {}).get("attach_to"):
            pen.setWidth(3)
            pen.setStyle(Qt.PenStyle.DashDotLine)
        self.setPen(pen)

    def itemChange(self, change, value):
        # 선택되면 내부를 살짝 어둡게 채워 선택 상태를 표시한다.
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            self.setBrush(QBrush(QColor(0, 0, 0, 60)) if value else QBrush())
        return super().itemChange(change, value)

    # ---------- 가장자리 드래그 리사이즈 ----------

    def _edges_at(self, pos) -> str:
        r = self.rect()
        g = self.EDGE_GRAB_PX
        edges = ""
        if abs(pos.y() - r.top()) <= g:
            edges += "T"
        elif abs(pos.y() - r.bottom()) <= g:
            edges += "B"
        if abs(pos.x() - r.left()) <= g:
            edges += "L"
        elif abs(pos.x() - r.right()) <= g:
            edges += "R"
        return edges

    _CURSOR_BY_EDGES = {
        "L": Qt.CursorShape.SizeHorCursor,
        "R": Qt.CursorShape.SizeHorCursor,
        "T": Qt.CursorShape.SizeVerCursor,
        "B": Qt.CursorShape.SizeVerCursor,
        "TL": Qt.CursorShape.SizeFDiagCursor,
        "BR": Qt.CursorShape.SizeFDiagCursor,
        "TR": Qt.CursorShape.SizeBDiagCursor,
        "BL": Qt.CursorShape.SizeBDiagCursor,
    }

    def hoverMoveEvent(self, event) -> None:
        edges = self._edges_at(event.pos())
        self.setCursor(self._CURSOR_BY_EDGES.get(edges, Qt.CursorShape.ArrowCursor))
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.unsetCursor()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._resize_edges = self._edges_at(event.pos())
        if not self._resize_edges:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not self._resize_edges:
            super().mouseMoveEvent(event)
            return
        r = QRectF(self.rect())
        p = event.pos()
        if "L" in self._resize_edges:
            r.setLeft(min(p.x(), r.right() - 5))
        if "R" in self._resize_edges:
            r.setRight(max(p.x(), r.left() + 5))
        if "T" in self._resize_edges:
            r.setTop(min(p.y(), r.bottom() - 5))
        if "B" in self._resize_edges:
            r.setBottom(max(p.y(), r.top() + 5))
        self.setRect(r)

    def mouseReleaseEvent(self, event) -> None:
        if self._resize_edges:
            self._resize_edges = ""
            self.commit_bbox()
            return
        super().mouseReleaseEvent(event)

    def commit_bbox(self) -> None:
        """현재 rect를 정규화 좌표로 block['bbox']에 반영한다."""
        r = self.rect()
        self.block["bbox"] = [
            r.x() / self.page_w,
            r.y() / self.page_h,
            r.width() / self.page_w,
            r.height() / self.page_h,
        ]
        if self.on_dirty is not None:
            self.on_dirty()


class PageView(QGraphicsView):
    """일반 모드는 러버밴드 다중선택, add_mode일 땐 드래그로 새 블록을 그린다."""

    def __init__(self, scene: QGraphicsScene):
        super().__init__(scene)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.add_mode = False
        self.attach_mode = False  # 블록 연결 대상 지정 모드
        self.on_new_block = None
        self.on_attach_click = None  # 지정 모드에서 블록 클릭 시 호출 (BlockItem 또는 None)
        self.on_attach_cancel = None  # Esc로 지정 모드 취소
        self._drag_start = None
        self._drag_rect_item: QGraphicsRectItem | None = None

    def keyPressEvent(self, event) -> None:
        if self.attach_mode and event.key() == Qt.Key.Key_Escape:
            if self.on_attach_cancel:
                self.on_attach_cancel()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if self.attach_mode and event.button() == Qt.MouseButton.LeftButton:
            block_item = next(
                (it for it in self.items(event.pos()) if isinstance(it, BlockItem)), None
            )
            if self.on_attach_click:
                self.on_attach_click(block_item)
            return
        if self.add_mode and event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = self.mapToScene(event.pos())
            self._drag_rect_item = QGraphicsRectItem(QRectF(self._drag_start, self._drag_start))
            self._drag_rect_item.setPen(QPen(QColor(0, 0, 0), 1, Qt.PenStyle.DashLine))
            self.scene().addItem(self._drag_rect_item)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.add_mode and self._drag_rect_item is not None:
            cur = self.mapToScene(event.pos())
            self._drag_rect_item.setRect(QRectF(self._drag_start, cur).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self.add_mode and self._drag_rect_item is not None:
            rect = self._drag_rect_item.rect()
            self.scene().removeItem(self._drag_rect_item)
            self._drag_rect_item = None
            if rect.width() > 5 and rect.height() > 5 and self.on_new_block:
                self.on_new_block(rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height())
            return
        super().mouseReleaseEvent(event)


class ReviewWindow(QMainWindow):
    def __init__(self, pdf_path: Path, book_id: str, page_range: range):
        super().__init__()
        self.setWindowTitle(f"MathFlow 블록 검토 — {book_id}")
        self.pdf_path = pdf_path
        self.book_id = book_id
        self.page_range = page_range

        self.project_dir = REPO_ROOT / "editor" / "projects" / book_id
        self.output_dir = REPO_ROOT / "editor" / "output" / book_id
        self.cache = pipeline.BlockCache(self.project_dir / "vlm_cache.json")
        self.backend = OllamaBackend()

        self.pages_blocks: dict[int, list[dict]] = {}
        self.pages_wh: dict[int, tuple[int, int]] = {}
        self.status_path = self.output_dir / "status.json"
        self.page_status: dict[int, str] = metadata.load_status(self.status_path)
        self._load_existing_output()

        self.last_page_path = self.output_dir / "last_page.json"
        last_page = metadata.load_last_page(self.last_page_path)
        last_unit = units_module.unit_containing(last_page) if last_page is not None else None
        if last_unit is not None:
            self.page_range = last_unit.page_range
            self.current_page = last_page
        else:
            self.current_page = page_range.start
        self._page_w = 0
        self._page_h = 0
        self.dirty = False  # 마지막 저장 이후 아직 저장 안 된 변경이 있는지

        self._build_ui()
        self._load_page(self.current_page)

    # ---------- UI 구성 ----------

    def _build_ui(self) -> None:
        self.scene = QGraphicsScene()
        self.view = PageView(self.scene)
        self.view.on_new_block = self._on_new_block_drawn
        self.view.on_attach_click = self._on_attach_click
        self.view.on_attach_cancel = self._cancel_attach_by_user

        # 블록 연결 상태: 지정 모드 소스 블록, 연결선 아이템들
        # (id→BlockItem 맵은 캐시하지 않고 씬에서 그때그때 만든다 — _block_item_map)
        self._attach_source: BlockItem | None = None
        self._attach_lines: list[QGraphicsLineItem] = []

        self._build_menus()
        self._build_toolbar()
        self._build_type_shortcuts()

        # 버튼 행(FlowLayout)을 뷰 위에 세로로 쌓는다 — QToolBar를 여러 개
        # addToolBar()로 나열하면 창이 좁아져도 줄바꿈이 안 되고 창 자체의
        # 최소 폭이 늘어나 버리는 걸 확인해서(flow_layout.py 모듈 docstring
        # 참고) 일반 QWidget+FlowLayout으로 대체했다.
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.toolbar_widget)
        central_layout.addWidget(self.view)
        self.setCentralWidget(central)

        # 뷰포트의 자식으로 붙이면 스크롤 시 같이 밀려 올라가므로(QWidget.scroll은
        # 자식 위젯도 이동시킴) 스크롤 영향이 없는 뷰 프레임에 붙인다.
        self.legend = QLabel(_build_legend_html(), self.view)
        # 시스템 다크 모드에선 기본 글자색이 흰색이라 배경에 묻힌다 — 색을 못 박는다.
        self.legend.setStyleSheet(
            "background-color: rgba(20, 20, 20, 235); color: #ffffff;"
            "border: 1px solid #666; border-radius: 4px; padding: 4px;"
        )
        self.legend.adjustSize()
        # 스크롤바 등장/소멸로 뷰포트 폭이 바뀌는 건 창 resizeEvent에 안 잡히므로
        # 뷰포트 리사이즈를 직접 감지해서 범례를 재배치한다.
        self.view.viewport().installEventFilter(self)
        self.legend_action.toggled.connect(self.legend.setVisible)

        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_label)

    def _make_tool_button(self, action: QAction) -> QToolButton:
        btn = QToolButton()
        btn.setDefaultAction(action)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        return btn

    def _add_tool_action(self, label: str, shortcut=None, callback=None, checkable: bool = False) -> QAction:
        """FlowLayout 버튼 행에 버튼 하나를 추가한다.

        label에는 단축키를 괄호로 미리 병기해서 넘긴다(예: "병합 (Ctrl+M)") —
        버튼에 표시되는 글자가 곧 label이라 따로 포맷할 필요가 없다. checkable=True
        인데 callback이 없으면(예: 범례 토글처럼 대상 위젯이 아직 안 만들어졌을
        때) 액션만 만들어 두고 연결은 호출한 쪽에서 나중에 한다.
        """
        action = QAction(label, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        if checkable:
            action.setCheckable(True)
            if callback is not None:
                action.toggled.connect(callback)
        elif callback is not None:
            action.triggered.connect(callback)
        self._toolbar_layout.addWidget(self._make_tool_button(action))
        return action

    def _add_separator(self) -> None:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        self._toolbar_layout.addWidget(line)

    def _build_toolbar(self) -> None:
        self.toolbar_widget = QWidget()
        self._toolbar_layout = FlowLayout(self.toolbar_widget, margin=4, h_spacing=6, v_spacing=4)
        # QVBoxLayout이 (줄바꿈으로 늘어난) 실제 필요 높이를 알아야 뷰를 안 가리고
        # 제대로 밀어내린다 — widget에 layout만 달아둔 것으로는 부족하고, 폭에 따라
        # 높이가 달라진다는 것(heightForWidth)을 sizePolicy에도 명시해야 한다.
        size_policy = self.toolbar_widget.sizePolicy()
        size_policy.setHeightForWidth(True)
        self.toolbar_widget.setSizePolicy(size_policy)

        self.page_spin = QSpinBox()
        self.page_spin.setRange(self.page_range.start, self.page_range.stop - 1)
        self.page_spin.setValue(self.current_page)
        self.page_spin.valueChanged.connect(self._on_page_spin_changed)
        self._toolbar_layout.addWidget(QLabel(" 페이지 "))
        self._toolbar_layout.addWidget(self.page_spin)

        self._add_tool_action(
            "◀ 이전 (←)",
            Qt.Key.Key_Left,
            lambda: self.page_spin.setValue(max(self.page_range.start, self.current_page - 1)),
        )
        self._add_tool_action(
            "다음 ▶ (→)",
            Qt.Key.Key_Right,
            lambda: self.page_spin.setValue(min(self.page_range.stop - 1, self.current_page + 1)),
        )

        self._add_separator()
        self.type_combo = QComboBox()
        self.type_combo.addItems(BLOCK_TYPES)
        self.type_combo.activated.connect(lambda _: self._on_type_changed(self.type_combo.currentText()))
        self._toolbar_layout.addWidget(QLabel(" 선택 블록 타입: "))
        self._toolbar_layout.addWidget(self.type_combo)
        self.scene.selectionChanged.connect(self._sync_type_combo)

        self._add_tool_action("병합 (Ctrl+M)", "Ctrl+M", self._merge_selected)
        self._add_tool_action("삭제 (Delete)", Qt.Key.Key_Delete, self._delete_selected)
        self._add_tool_action("블록 연결 (Ctrl+L)", "Ctrl+L", self._start_attach_selected)
        # 예전엔 단축키가 없어 메뉴 클릭으로만 연결을 풀 수 있었다 — 병합/삭제/
        # 연결처럼 자주 쓰는 편집 동작에 단축키가 없는 게 불편해서 추가.
        self._add_tool_action("연결 해제 (Ctrl+U)", "Ctrl+U", self._detach_selected)

        self.add_action = self._add_tool_action("새 블록 추가 (N)", "N", self._toggle_add_mode, checkable=True)

        self._add_separator()
        # "다음 검토 필요 페이지"도 마우스 클릭만 가능했던 버튼 — R(review) 단축키 추가.
        self._add_tool_action("다음 검토 필요 페이지 (R)", "R", self._jump_next_needs_review)
        self._add_tool_action("저장 (S)", "S", self._save)

        self._add_separator()
        self.done_action = self._add_tool_action("이 페이지 완료 (D)", "D", self._toggle_done, checkable=True)
        # 범례 표시 토글 연결은 self.legend가 만들어진 뒤 _build_ui에서 이어붙인다.
        self.legend_action = self._add_tool_action("범례 (L)", "L", checkable=True)
        self.legend_action.setChecked(True)

    def _build_type_shortcuts(self) -> None:
        """숫자키로 선택된 블록의 타입을 바로 바꾼다.

        범례에 표시되는 번호(_TYPE_SHORTCUT_KEYS, 1부터 BLOCK_TYPES/_TYPE_QCOLOR
        순서)와 정확히 대응해야 사용자가 범례를 보고 누를 키를 알 수 있다.
        """
        for block_type, key in _TYPE_SHORTCUT_KEYS.items():
            action = QAction(f"타입 변경 {key}: {block_type}", self)
            action.setShortcut(key)
            action.triggered.connect(lambda checked=False, t=block_type: self._set_selected_type(t))
            self.addAction(action)

    def _build_menus(self) -> None:
        # Qt는 부모가 소유하면 살아있어야 하지만, PySide6에서 로컬 변수로만 들고 있으면
        # 파이썬 쪽 참조가 없어져 래퍼가 GC되는 경우가 있어 self에 붙잡아둔다.
        self.file_menu = self.menuBar().addMenu("파일")
        self.unit_menu = self.file_menu.addMenu("단원 열기")
        for unit in units_module.UNITS:
            action = self.unit_menu.addAction(f"{unit.title}  ({unit.start_page}~{unit.end_page}쪽)")
            action.triggered.connect(lambda checked=False, u=unit: self._open_unit(u))

        self.tools_menu = self.menuBar().addMenu("도구")
        self.tools_menu.addAction("새로 캐싱: 단원 내 완료→미완료").triggered.connect(
            self._open_recache_same_unit_dialog
        )
        self.tools_menu.addAction("새로 캐싱: 완료 단원 → 다른 단원").triggered.connect(
            self._open_recache_cross_unit_dialog
        )

        self.transfer_menu = self.menuBar().addMenu("전송")
        self.transfer_menu.addAction("서버로 전송...").triggered.connect(self._open_transfer_dialog)

    # ---------- 단원 전환 ----------

    def _open_unit(self, unit: units_module.Unit) -> None:
        if not self._confirm_discard_or_save():
            return
        self.page_range = unit.page_range
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(unit.start_page, unit.end_page)
        self.page_spin.setValue(unit.start_page)
        self.page_spin.blockSignals(False)
        self._load_page(unit.start_page)

    # ---------- 페이지 완료 상태 ----------

    def _toggle_done(self, checked: bool) -> None:
        self.page_status[self.current_page] = "done" if checked else "pending"
        metadata.save_status(self.page_status, self.status_path)

    # ---------- 새로 캐싱 ----------
    # 두 메뉴 모두 실제 재분석 로직(_run_recache)은 같다 — "완료 페이지를 참고해
    # 코드에 반영된 규칙을, 미완료 페이지에 다시 돌린다"는 동작 자체는 동일하고
    # 다른 건 "미완료 페이지를 어느 범위에서 고르느냐"뿐이라, 이를 선택 UI로 나눴다.
    # (완료 페이지 자체를 알고리즘에 입력으로 쓰는 학습 같은 건 아니다 — 사람이
    # diff를 보고 규칙을 코드에 반영하는 건 여전히 대화로 하는 별도 단계다.)

    def _unit_combo(self, label_fmt) -> QComboBox:
        combo = QComboBox()
        for unit in units_module.UNITS:
            done = sum(1 for p in unit.page_range if self.page_status.get(p) == "done")
            combo.addItem(label_fmt(unit, done, len(unit.page_range)), unit)
        return combo

    def _open_recache_same_unit_dialog(self) -> None:
        """단원 내 완료 페이지를 기준으로, 같은 단원의 미완료 페이지만 다시 분석."""
        dialog = QDialog(self)
        dialog.setWindowTitle("새로 캐싱 — 단원 내")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("완료 페이지를 기준으로, 같은 단원의 미완료 페이지를 다시 분석합니다."))

        combo = self._unit_combo(lambda u, done, total: f"{u.title}  (완료 {done}/{total})")
        current_unit = units_module.unit_containing(self.current_page)
        if current_unit is not None:
            idx = combo.findData(current_unit)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        layout.addWidget(combo)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("실행")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._run_recache([combo.currentData()])

    def _open_recache_cross_unit_dialog(self) -> None:
        """기준 단원의 완료 상태를 참고 삼아, 선택한 다른 단원들의 미완료 페이지를 다시 분석."""
        dialog = QDialog(self)
        dialog.setWindowTitle("새로 캐싱 — 다른 단원에 적용")
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel("기준 단원의 완료 페이지를 참고해, 선택한 다른 단원들의 미완료 페이지를 다시 분석합니다.")
        )

        layout.addWidget(QLabel("기준 단원 (완료 페이지가 많을수록 안정적):"))
        source_combo = self._unit_combo(lambda u, done, total: f"{u.title}  (완료 {done}/{total})")
        layout.addWidget(source_combo)

        layout.addWidget(QLabel("적용 대상 단원 (미완료 페이지를 다시 분석 — 완료 페이지는 안 건드림):"))
        target_list = QListWidget()
        for unit in units_module.UNITS:
            pending = sum(1 for p in unit.page_range if self.page_status.get(p) != "done")
            item = QListWidgetItem(f"{unit.title}  (미완료 {pending}개)")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, unit)
            target_list.addItem(item)
        layout.addWidget(target_list)

        def _uncheck_source(_index: int = 0) -> None:
            # 기준 단원을 대상으로도 체크해두면 "완료->미완료"가 자기 자신을 가리켜
            # 혼란스러우니, 기준으로 고른 단원은 대상 목록에서 자동으로 해제한다.
            src = source_combo.currentData()
            for i in range(target_list.count()):
                item = target_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) is src:
                    item.setCheckState(Qt.CheckState.Unchecked)

        source_combo.currentIndexChanged.connect(_uncheck_source)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("실행")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        source_unit = source_combo.currentData()
        target_units = [
            target_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(target_list.count())
            if target_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        if not target_units:
            QMessageBox.information(self, "새로 캐싱", "적용 대상 단원을 선택하세요.")
            return

        source_done = sum(1 for p in source_unit.page_range if self.page_status.get(p) == "done")
        if source_done == 0:
            reply = QMessageBox.question(
                self,
                "새로 캐싱",
                f"기준 단원 '{source_unit.title}'에 완료 표시된 페이지가 없습니다. 그래도 진행할까요?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._run_recache(target_units)

    def _run_recache(self, units: list[units_module.Unit]) -> None:
        pending_pages = [
            (u, p) for u in units for p in u.page_range if self.page_status.get(p) != "done"
        ]
        if not pending_pages:
            QMessageBox.information(self, "새로 캐싱", "선택한 단원에 미완료 페이지가 없습니다.")
            return

        progress = QProgressDialog("페이지 목록 확인 중...", "취소", 0, 1, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QApplication.processEvents()

        # 1단계: 블록 개수만 먼저 센다 (로컬 CV 세그멘테이션만, VLM 호출 없어서 빠름).
        # 이걸로 전체 진행률의 분모(총 블록 수)를 구해야 "페이지 1/9"가 아니라
        # 실제 작업량 기준 % 가 나온다.
        page_plan: list[tuple[units_module.Unit, int, int]] = []
        for unit, page_number in pending_pages:
            if progress.wasCanceled():
                return
            img = segment.render_page(self.pdf_path, page_number - 1, 150)
            block_count = len(segment.detect_blocks(img))
            page_plan.append((unit, page_number, block_count))
            QApplication.processEvents()

        total_blocks = sum(c for _, _, c in page_plan) or 1
        progress.setRange(0, total_blocks)

        done_blocks = 0
        cancelled = False
        pages_done = 0
        start_time = time.monotonic()
        for unit, page_number, block_count in page_plan:
            if progress.wasCanceled():
                cancelled = True
                break

            def on_progress(i: int, total: int, unit=unit, page_number=page_number) -> None:
                n_done = done_blocks + i
                progress.setValue(n_done)
                pct = int(n_done / total_blocks * 100)
                # 첫 몇 블록은 평균이 안정되기 전이라 예상 시간이 크게 튈 수 있어
                # 어느 정도 진행된 뒤부터만 표시한다.
                eta_str = ""
                if n_done >= 3:
                    avg_per_block = (time.monotonic() - start_time) / n_done
                    remaining = avg_per_block * (total_blocks - n_done)
                    eta_str = f", 남은 시간 약 {_format_eta(remaining)}"
                progress.setLabelText(
                    f"{unit.title} — {page_number}쪽 블록 {i}/{total}  (전체 {pct}%{eta_str})"
                )
                QApplication.processEvents()

            entries = pipeline.run_page(
                self.pdf_path,
                page_index=page_number - 1,
                page_number=page_number,
                dpi=150,
                backend=self.backend,
                cache=self.cache,
                force=True,
                on_progress=on_progress,
                should_stop=progress.wasCanceled,
            )
            # 취소로 중간에 끊긴 페이지는 일부 블록만 분류된 반쪽짜리라 저장하지 않는다.
            if not progress.wasCanceled():
                self.pages_blocks[page_number] = entries
                pages_done += 1
            self.cache.save()
            done_blocks += block_count

        progress.setValue(total_blocks)

        if self.current_page in self.pages_blocks:
            self._load_page(self.current_page)
        self._save(silent=True)

        if cancelled:
            QMessageBox.information(
                self, "새로 캐싱", f"취소했습니다. 완료된 {pages_done}개 페이지는 저장했습니다."
            )
        else:
            QMessageBox.information(self, "새로 캐싱", f"{len(pending_pages)}개 페이지를 다시 분석하고 저장했습니다.")

    # ---------- 서버로 전송 ----------

    def _open_transfer_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("서버로 전송")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("전송할 단원을 고르세요. (해당 페이지들을 렌더링해 파이 서버로 올립니다)"))

        list_widget = QListWidget()
        for unit in units_module.UNITS:
            done = sum(1 for p in unit.page_range if self.page_status.get(p) == "done")
            item = QListWidgetItem(f"{unit.title}  (완료된 페이지 {done}/{len(unit.page_range)})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, unit)
            list_widget.addItem(item)
        layout.addWidget(list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("전송")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected_units = [
            list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(list_widget.count())
            if list_widget.item(i).checkState() == Qt.CheckState.Checked
        ]
        if selected_units:
            self._run_transfer(selected_units)

    def _run_transfer(self, units: list[units_module.Unit]) -> None:
        # 저장 안 된 변경이 있으면 먼저 저장부터 — 안 그러면 화면에만 있는 수정이
        # 서버로 안 올라간다.
        if self.dirty and not self._save(silent=True):
            return

        pages = [p for u in units for p in u.page_range]

        progress = QProgressDialog("페이지 렌더링 중...", "취소", 0, len(pages) + 1, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        export.ensure_book_json(self.output_dir, self.book_id, BOOK_TITLE, BOOK_PAGE_COUNT)

        for i, page_number in enumerate(pages):
            progress.setValue(i)
            progress.setLabelText(f"{page_number}쪽 렌더링 중... ({i + 1}/{len(pages)})")
            if progress.wasCanceled():
                return
            export.render_page_images(self.pdf_path, self.output_dir, [page_number])

        progress.setLabelText("서버로 전송 중...")
        progress.setValue(len(pages))

        result = export.transfer_to_server(self.output_dir, self.book_id)
        progress.setValue(len(pages) + 1)

        if result.returncode != 0:
            QMessageBox.critical(self, "전송 실패", result.stderr or "알 수 없는 오류")
            return
        QMessageBox.information(self, "전송 완료", f"{len(pages)}개 페이지를 서버로 전송했습니다.")

    # ---------- 데이터 로딩 ----------

    def _load_existing_output(self) -> None:
        blocks_path = self.output_dir / "blocks.json"
        if blocks_path.exists():
            data = metadata.load_blocks(blocks_path)
            for b in data["blocks"]:
                self.pages_blocks.setdefault(b["page"], []).append({"block": b, "needs_review": False})
            self._refresh_needs_review_flags()

        pages_path = self.output_dir / "pages.json"
        if pages_path.exists():
            data = metadata.load_pages(pages_path)
            for p in data["pages"]:
                self.pages_wh[p["number"]] = (p["width_px"], p["height_px"])

    def _refresh_needs_review_flags(self) -> None:
        for page, blist in self.pages_blocks.items():
            flags = review.flag_needs_review([e["block"] for e in blist])
            for e, f in zip(blist, flags):
                e["needs_review"] = f

    def _get_page_entries(self, page_number: int) -> list[dict]:
        """이미 분석/저장된 블록만 돌려준다 — 여기서 VLM을 새로 부르지 않는다.

        예전엔 캐시에 없는 페이지를 만나면 그 자리에서 pipeline.run_page()를
        돌렸는데, 그냥 다음 페이지가 어떻게 생겼는지 보려고 넘겼을 뿐인데도
        블록마다 VLM 호출이 걸려서(큰 블록은 1분 넘게 걸리기도 함, 33쪽 실측)
        화면이 그 시간 내내 멎었다("응답 없음"). 아직 분석 안 된 페이지는
        블록 없이 그림만 먼저 보여주고, 실제 분석은 "새로 캐싱" 메뉴로 명시적
        으로 하게 한다(이미 그 경로에 진행 다이얼로그가 붙어 있음).
        """
        entries = self.pages_blocks.get(page_number, [])
        if entries:
            flags = review.flag_needs_review([e["block"] for e in entries])
            for e, f in zip(entries, flags):
                e["needs_review"] = f
        return entries

    def _load_page(self, page_number: int) -> None:
        self.current_page = page_number
        metadata.save_last_page(page_number, self.last_page_path)
        img = segment.render_page(self.pdf_path, page_number - 1, 150)
        h, w = img.shape[:2]
        self._page_w, self._page_h = w, h
        self.pages_wh[page_number] = (w, h)

        self._cancel_attach()  # 페이지가 바뀌면 진행 중이던 지정 모드는 취소
        self.scene.clear()
        self._attach_lines = []
        pix_item = QGraphicsPixmapItem(_cv_to_qpixmap(img))
        pix_item.setZValue(-1)
        self.scene.addItem(pix_item)
        self.scene.setSceneRect(0, 0, w, h)

        for e in self._get_page_entries(page_number):
            self.scene.addItem(self._make_block_item(e["block"], e["needs_review"]))

        self._redraw_attachment_lines()  # 기존 연결을 중심-중심 선으로 표시

        self.done_action.blockSignals(True)
        self.done_action.setChecked(self.page_status.get(page_number) == "done")
        self.done_action.blockSignals(False)

        unit = units_module.unit_containing(page_number)
        title_suffix = f" · {unit.title}" if unit else ""
        self.setWindowTitle(f"MathFlow 블록 검토 — {self.book_id}{title_suffix}")

        self._fit_page()
        self._update_status()

    def _fit_page(self) -> None:
        """페이지 가로 폭을 뷰에 꽉 채운다(세로는 스크롤). 창 크기 변경 시 재호출."""
        rect = self.scene.sceneRect()
        if rect.isEmpty():
            return
        self.view.resetTransform()
        # 세로 스크롤바 폭만큼 빼고 맞춰야 가로 스크롤바가 안 생긴다.
        scrollbar_w = self.view.verticalScrollBar().sizeHint().width()
        scale = (self.view.viewport().width() + self.view.frameWidth() * 2 - scrollbar_w) / rect.width()
        self.view.scale(scale, scale)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.view.viewport() and event.type() == event.Type.Resize:
            self._place_legend()
        return super().eventFilter(obj, event)

    def _place_legend(self) -> None:
        """범례를 뷰 오른쪽 위 구석(스크롤바 안쪽)에 붙인다."""
        margin = 8
        frame = self.view.frameWidth()
        self.legend.adjustSize()  # 표시 전엔 폭이 확정되지 않으므로 배치 직전에 재계산
        self.legend.move(
            frame + self.view.viewport().width() - self.legend.width() - margin,
            frame + margin,
        )
        self.legend.raise_()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 생성자 시점엔 뷰 크기가 확정 전이라 여기서 다시 맞춰야 실제 창 크기에 맞는다.
        self._fit_page()
        self._place_legend()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_page()
        self._place_legend()

    def _update_status(self) -> None:
        if self.current_page not in self.pages_blocks:
            self.status_label.setText(f"{self.current_page}쪽 · 아직 분석 안 됨 (도구 메뉴 → 새로 캐싱)")
            return
        entries = self.pages_blocks[self.current_page]
        nr = sum(e["needs_review"] for e in entries)
        done = "완료" if self.page_status.get(self.current_page) == "done" else "미완료"
        self.status_label.setText(f"{self.current_page}쪽 · 블록 {len(entries)}개 · 검토필요 {nr}개 · {done}")

    # ---------- 변경사항 추적 ----------

    def _mark_dirty(self) -> None:
        self.dirty = True

    def _confirm_discard_or_save(self) -> bool:
        """저장 안 된 변경이 있으면 저장/저장 안 함/취소를 묻는다. 진행해도 되면 True."""
        if not self.dirty:
            return True
        reply = QMessageBox.question(
            self,
            "변경사항이 있습니다",
            "저장하지 않은 변경사항이 있습니다. 저장할까요?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Save:
            return self._save(silent=True)
        if reply == QMessageBox.StandardButton.Discard:
            self._discard_changes()
            return True
        return False

    def _discard_changes(self) -> None:
        """마지막 저장 시점(디스크의 blocks.json/pages.json) 상태로 메모리를 되돌린다."""
        self.pages_blocks = {}
        self.pages_wh = {}
        self._load_existing_output()
        self.dirty = False

    def closeEvent(self, event) -> None:
        if self._confirm_discard_or_save():
            event.accept()
        else:
            event.ignore()

    # ---------- 이벤트 핸들러 ----------

    def _on_page_spin_changed(self, value: int) -> None:
        if value == self.current_page:
            return
        if not self._confirm_discard_or_save():
            self.page_spin.blockSignals(True)
            self.page_spin.setValue(self.current_page)
            self.page_spin.blockSignals(False)
            return
        self._load_page(value)

    def _selected_items(self) -> list[BlockItem]:
        return [it for it in self.scene.selectedItems() if isinstance(it, BlockItem)]

    def _sync_type_combo(self) -> None:
        """선택된 블록의 타입을 드롭다운에 반영한다. 타입이 섞여 있으면 그대로 둔다."""
        types = {it.block["type"] for it in self._selected_items()}
        if len(types) == 1:
            self.type_combo.setCurrentText(types.pop())

    def _on_type_changed(self, new_type: str) -> None:
        items = self._selected_items()
        for it in items:
            it.block["type"] = new_type
            it.block["reflow"] = {"role": pipeline.ROLE_BY_TYPE.get(new_type, "paragraph")}
            it.apply_style()
        if items:
            self._mark_dirty()

    def _set_selected_type(self, new_type: str) -> None:
        """숫자키 단축키로 선택된 블록의 타입을 바꾼다 (_TYPE_SHORTCUT_KEYS 참고)."""
        if not self._selected_items():
            return
        self.type_combo.setCurrentText(new_type)
        self._on_type_changed(new_type)

    _ID_NUM_RE = re.compile(r"(\d+)$")

    def _next_block_num(self, entries: list[dict]) -> int:
        """병합/새 블록에 쓸 id 번호를 정한다. len(entries)를 그대로 쓰면, 블록을
        지웠다가 나중에 다시 병합/새로 그릴 때 개수가 줄어든 만큼 번호가 재사용돼서
        서로 다른 두 블록이 같은 id를 갖는 충돌이 생겼다(16쪽에서 problem_number
        블록과 그림 블록이 둘 다 p16_bn23이 되어, 뷰어의 id→블록 딕셔너리 조회에서
        하나가 다른 하나를 덮어써 화면에 중복 렌더링/블록 유실이 난 걸로 발견).
        현재 남아있는 모든 블록 id의 번호 중 최댓값+1을 쓰면, 지워진 번호는 다시
        내주지 않으면서도 살아있는 id와는 절대 겹치지 않는다.
        """
        best = -1
        for e in entries:
            m = self._ID_NUM_RE.search(e["block"]["id"])
            if m:
                best = max(best, int(m.group(1)))
        return best + 1

    # ---------- 블록 연결 (지정 모드) ----------

    def _start_attach_selected(self) -> None:
        """툴바 '블록 연결': 선택된 소스 블록 하나로 지정 모드에 들어간다."""
        srcs = [it for it in self._selected_items() if it.block["type"] in _ATTACH_SOURCE_TYPES]
        if len(srcs) != 1:
            self.status_label.setText("블록 연결: 옮길 블록(figure/text/formula) 하나를 선택한 뒤 누르세요")
            return
        self._start_attach(srcs[0])

    def _start_attach(self, src_item: BlockItem) -> None:
        """지정 모드 진입 — 이후 클릭한 블록을 이 소스의 연결 대상으로 삼는다."""
        self._attach_source = src_item
        self.view.attach_mode = True
        self.view.setCursor(Qt.CursorShape.CrossCursor)
        self.view.setFocus()  # Esc 취소가 뷰에 전달되도록 포커스를 준다
        self.status_label.setText("이 블록을 어디 뒤에 붙일지, 대상 블록을 클릭하세요 · Esc 취소")

    def _cancel_attach(self) -> None:
        if self._attach_source is None and not self.view.attach_mode:
            return
        self._attach_source = None
        self.view.attach_mode = False
        self.view.unsetCursor()

    def _cancel_attach_by_user(self) -> None:
        """Esc로 지정 모드를 취소 — 상태표시도 원래대로 되돌린다."""
        active = self._attach_source is not None
        self._cancel_attach()
        if active:
            self._update_status()

    def _on_attach_click(self, target_item: BlockItem | None) -> None:
        """지정 모드에서 대상 블록을 클릭했을 때 연결 확정."""
        src = self._attach_source
        if src is None:
            return
        if target_item is None or target_item is src:
            return  # 빈 곳/자기 자신은 무시하고 계속 대기
        src.block.setdefault("reflow", {})["attach_to"] = target_item.block["id"]
        src.apply_style()
        self._mark_dirty()
        self._cancel_attach()
        self._redraw_attachment_lines()
        self.status_label.setText(f"블록을 {target_item.block['id']} 뒤에 연결했습니다 (저장 후 반영)")

    def _detach_selected(self) -> None:
        """툴바 '연결 해제': 선택된 블록들의 연결을 푼다."""
        # list comprehension으로 먼저 전부 처리한다 — any(제너레이터)는 첫 True에서
        # 멈춰서 여러 개 선택 시 나머지가 해제 안 되는 버그가 된다.
        cleared = [self._clear_attach(it) for it in self._selected_items()]
        if any(cleared):
            self.status_label.setText("연결을 해제했습니다 (저장 후 반영)")

    def _detach_item(self, fig_item: BlockItem) -> None:
        """우클릭 메뉴 '연결 해제'."""
        if self._clear_attach(fig_item):
            self.status_label.setText("연결을 해제했습니다 (저장 후 반영)")

    def _clear_attach(self, item: BlockItem) -> bool:
        reflow = item.block.get("reflow") or {}
        if reflow.pop("attach_to", None) is None:
            return False
        item.apply_style()
        self._mark_dirty()
        self._redraw_attachment_lines()
        return True

    def _make_block_item(self, block: dict, needs_review: bool) -> BlockItem:
        """BlockItem을 만들고 콜백을 붙인다. 병합/새로 그린 블록도 반드시 이걸 거쳐야
        우클릭 '블록 연결'이 동작한다 (예전엔 on_dirty만 붙여서 새 블록은 메뉴가 먹통이었다)."""
        item = BlockItem(block, needs_review, self._page_w, self._page_h)
        item.on_dirty = self._mark_dirty
        item.on_request_attach = self._start_attach
        item.on_request_detach = self._detach_item
        return item

    def _block_item_map(self) -> dict[str, BlockItem]:
        """id→BlockItem. 씬에서 그때그때 만든다 — 캐시해두면 병합/삭제/새 블록 뒤에
        낡아서(새 블록이 없고 사라진 블록이 남아) 연결선이 안 그려진다."""
        return {it.block["id"]: it for it in self.scene.items() if isinstance(it, BlockItem)}

    def _repoint_attachments(self, old_ids: set[str], new_id: str | None) -> None:
        """old_ids를 가리키던 attach_to를 new_id로 옮긴다(new_id가 None이면 연결 해제).

        대상 블록이 병합되면 그 후속(병합 결과)으로 연결을 넘기고, 삭제되면 연결을
        푼다 — 안 그러면 attach_to가 죽은 id를 가리킨 채 남는다.
        """
        items = self._block_item_map()
        for e in self.pages_blocks[self.current_page]:
            reflow = e["block"].get("reflow") or {}
            if reflow.get("attach_to") not in old_ids:
                continue
            if new_id:
                reflow["attach_to"] = new_id
            else:
                reflow.pop("attach_to", None)
            it = items.get(e["block"]["id"])
            if it is not None:
                it.apply_style()  # 연결 표시(굵은 점선-대시) 갱신

    def _redraw_attachment_lines(self) -> None:
        """attach_to로 연결된 (소스→대상) 쌍마다 중심-중심 선을 다시 그린다."""
        for ln in self._attach_lines:
            self.scene.removeItem(ln)
        self._attach_lines = []
        items = self._block_item_map()
        for item in items.values():
            tid = (item.block.get("reflow") or {}).get("attach_to")
            target = items.get(tid) if tid else None
            if target is None:
                continue
            ax, ay = _block_center(item.block, item.page_w, item.page_h)
            bx, by = _block_center(target.block, target.page_w, target.page_h)
            ln = QGraphicsLineItem(ax, ay, bx, by)
            pen = QPen(QColor(230, 120, 20))
            pen.setCosmetic(True)  # 화면 배율과 무관하게 일정 두께
            pen.setWidth(2)
            ln.setPen(pen)
            ln.setZValue(5)
            ln.setAcceptedMouseButtons(Qt.MouseButton.NoButton)  # 클릭이 통과되게
            self.scene.addItem(ln)
            self._attach_lines.append(ln)

    def _merge_selected(self) -> None:
        items = self._selected_items()
        if len(items) < 2:
            return
        self._cancel_attach()  # 지정 모드 중이면 소스가 사라질 수 있으니 취소
        blocks = [it.block for it in items]
        x0 = min(b["bbox"][0] for b in blocks)
        y0 = min(b["bbox"][1] for b in blocks)
        x1 = max(b["bbox"][0] + b["bbox"][2] for b in blocks)
        y1 = max(b["bbox"][1] + b["bbox"][3] for b in blocks)
        biggest = max(blocks, key=lambda b: b["bbox"][2] * b["bbox"][3])

        entries = self.pages_blocks[self.current_page]
        merged_ids = {b["id"] for b in blocks}
        for it in items:
            entries.remove(next(e for e in entries if e["block"] is it.block))
            self.scene.removeItem(it)

        new_block = {
            "id": f"p{self.current_page}_bm{self._next_block_num(entries):02d}",
            "page": self.current_page,
            "type": biggest["type"],
            "bbox": [x0, y0, x1 - x0, y1 - y0],
            "order": len(entries),
            "reflow": {"role": pipeline.ROLE_BY_TYPE.get(biggest["type"], "paragraph")},
        }
        entries.append({"block": new_block, "needs_review": False})
        self.scene.addItem(self._make_block_item(new_block, False))
        # 병합된 블록을 가리키던 연결은 병합 결과로 넘긴다 (죽은 id로 남지 않게)
        self._repoint_attachments(merged_ids, new_block["id"])
        self._redraw_attachment_lines()
        self._mark_dirty()
        self._update_status()

    def _delete_selected(self) -> None:
        entries = self.pages_blocks[self.current_page]
        items = self._selected_items()
        if not items:
            self._update_status()
            return
        self._cancel_attach()  # 지정 모드 중이면 소스가 사라질 수 있으니 취소
        deleted_ids = {it.block["id"] for it in items}
        for it in items:
            entries.remove(next(e for e in entries if e["block"] is it.block))
            self.scene.removeItem(it)
        # 삭제된 블록을 가리키던 연결은 풀어준다 (죽은 id로 남지 않게)
        self._repoint_attachments(deleted_ids, None)
        self._redraw_attachment_lines()
        self._mark_dirty()
        self._update_status()

    def _toggle_add_mode(self, checked: bool) -> None:
        self.view.add_mode = checked
        self.view.setDragMode(
            QGraphicsView.DragMode.NoDrag if checked else QGraphicsView.DragMode.RubberBandDrag
        )

    def _on_new_block_drawn(self, x0: float, y0: float, x1: float, y1: float) -> None:
        w, h = self._page_w, self._page_h
        entries = self.pages_blocks[self.current_page]
        new_block = {
            "id": f"p{self.current_page}_bn{self._next_block_num(entries):02d}",
            "page": self.current_page,
            "type": "text",
            "bbox": [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h],
            "order": len(entries),
            "reflow": {"role": "paragraph"},
        }
        entries.append({"block": new_block, "needs_review": False})
        self.scene.addItem(self._make_block_item(new_block, False))
        self.add_action.setChecked(False)
        self._mark_dirty()
        self._update_status()

    def _jump_next_needs_review(self) -> None:
        n = self.page_range.stop - self.page_range.start
        for i in range(1, n + 1):
            candidate = self.page_range.start + (self.current_page - self.page_range.start + i) % n
            entries = self._get_page_entries(candidate)
            if any(e["needs_review"] for e in entries):
                self.page_spin.setValue(candidate)
                return
        QMessageBox.information(self, "검토", "검토가 필요한 블록이 더 없습니다.")

    def _save(self, silent: bool = False) -> bool:
        """blocks.json/pages.json에 저장한다. 성공하면 True.

        silent=True는 "변경사항 있음" 다이얼로그에서 저장을 고르거나 재캐싱 뒤
        자동 저장할 때처럼, 저장 자체가 사용자가 요청한 주된 동작이 아닐 때 쓴다
        (매번 "저장했습니다" 팝업까지 뜨면 페이지 넘길 때마다 클릭이 두 번 필요해진다).
        """
        # 병합/새 블록은 항상 리스트 맨 끝에 붙기 때문에, 화면에 있는 순서 그대로
        # 저장하면 리플로우 순서가 실제 화면 위치와 어긋난다 — 저장 직전에 실제
        # 위치(컬럼 → 위에서 아래) 기준으로 다시 정렬한다.
        for p in self.pages_blocks:
            self.pages_blocks[p].sort(key=lambda e: _reading_order_key(e["block"]))
            # 위치 기준 정렬 뒤, "블록 연결"로 지정된 블록을 대상 바로 뒤로 옮긴다.
            self.pages_blocks[p] = _apply_attachments(self.pages_blocks[p])
            for i, e in enumerate(self.pages_blocks[p]):
                e["block"]["order"] = i

        all_blocks = [e["block"] for p in sorted(self.pages_blocks) for e in self.pages_blocks[p]]
        blocks_data = {"schema_version": "1.0", "book_id": self.book_id, "blocks": all_blocks}

        # 이번 세션에서 화면에 띄운 적 없는 페이지(이전 저장분만 있는 경우)는
        # width/height를 얻으려고 렌더링만 한 번 해준다 (VLM 재호출 없음).
        for p in self.pages_blocks:
            if p not in self.pages_wh:
                img = segment.render_page(self.pdf_path, p - 1, 150)
                h, w = img.shape[:2]
                self.pages_wh[p] = (w, h)

        pages_data = {
            "schema_version": "1.0",
            "book_id": self.book_id,
            "pages": [
                {
                    "number": p,
                    "width_px": self.pages_wh[p][0],
                    "height_px": self.pages_wh[p][1],
                    "block_order": [e["block"]["id"] for e in self.pages_blocks[p]],
                }
                for p in sorted(self.pages_blocks)
            ],
        }

        try:
            metadata.save_blocks(blocks_data, self.output_dir / "blocks.json")
            metadata.save_pages(pages_data, self.output_dir / "pages.json")
        except Exception as exc:  # noqa: BLE001 - 사용자에게 그대로 보여줄 검증 오류
            QMessageBox.critical(self, "저장 실패", f"스키마 검증 실패:\n{exc}")
            return False

        self.dirty = False
        if not silent:
            # 저장할 때마다 확인 클릭을 한 번씩 더 해야 하는 모달 대신, 상태바에
            # 몇 초 떴다 사라지는 메시지로 (저장은 자주 누르는 동작이라 매번
            # 클릭을 요구하면 거슬린다).
            self.statusBar().showMessage(f"저장했습니다 — {self.output_dir}", 3000)
        return True
