"""블록 검토/보정 화면.

segment+VLM 파이프라인이 만든 블록을 페이지 이미지 위에 색상으로 겹쳐 보여주고,
타입 수정/병합/삭제/새 블록 추가만 지원한다 (리사이즈 드래그 핸들은 v1에서 생략 —
필요하면 삭제 후 새로 그리는 것으로 대체). "다음 검토 필요 페이지"는 VLM confidence가
아니라 같은 페이지·같은 타입 블록 대비 면적이 비정상적으로 큰 블록(병합 실패 의심,
review.py)을 기준으로 삼는다 — VLM 자체 확신도는 실측상 신호가 안 됐기 때문이다.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
)

from .. import units as units_module
from ..analysis import pipeline, review, segment
from ..analysis.vlm_client import BLOCK_TYPES, OllamaBackend
from ..io import export, metadata

REPO_ROOT = Path(__file__).resolve().parents[3]

BOOK_TITLE = "공통수학2"
BOOK_PAGE_COUNT = 304

_TYPE_QCOLOR = {
    "text": QColor(230, 140, 0),
    "figure": QColor(0, 150, 0),
    "formula": QColor(30, 30, 220),
    "table": QColor(170, 0, 170),
    "problem_number": QColor(0, 150, 150),
}

_TYPE_LABEL_KO = {
    "text": "텍스트",
    "figure": "그림·그래프",
    "formula": "수식",
    "table": "표",
    "problem_number": "문제번호",
}

# 이 책의 페이지는 본문(왼쪽 ~70%)과 사이드바(오른쪽)로 나뉜다 — 읽는 순서는
# 컬럼별로 먼저 묶고 그 안에서 위에서 아래로. x=0.5를 기준으로 삼는 건 대략적인
# 근사치지만, 다른 정교한 컬럼 검출 없이도 "병합/새 블록이 리스트 끝에 붙어서
# 리플로우 순서가 화면 위치와 어긋나는" 문제는 이걸로 충분히 해결된다.
_COLUMN_SPLIT_X = 0.5


def _reading_order_key(block: dict) -> tuple[int, float]:
    x, y, _w, _h = block["bbox"]
    column = 1 if x > _COLUMN_SPLIT_X else 0
    return (column, y)


def _build_legend_html() -> str:
    rows = []
    for t, color in _TYPE_QCOLOR.items():
        # 어두운 범례 배경에서도 잘 보이도록 견본색만 살짝 밝힌다 (블록 테두리 원색 유지).
        swatch = color.lighter(140).name()
        rows.append(
            f'<tr><td style="color:{swatch};font-size:15px;">■</td>'
            f'<td style="padding-left:4px;">{_TYPE_LABEL_KO[t]}</td></tr>'
        )
    rows.append('<tr><td style="font-size:13px;">┄</td><td style="padding-left:4px;">검토 필요 (점선)</td></tr>')
    return f'<table style="margin:2px;">{"".join(rows)}</table>'


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
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.apply_style()

    def apply_style(self) -> None:
        color = _TYPE_QCOLOR.get(self.block["type"], QColor(120, 120, 120))
        pen = QPen(color, 2)
        if self.needs_review:
            pen.setStyle(Qt.PenStyle.DashLine)
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
        self.on_new_block = None
        self._drag_start = None
        self._drag_rect_item: QGraphicsRectItem | None = None

    def mousePressEvent(self, event) -> None:
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
        self.setCentralWidget(self.view)

        self._build_menus()

        toolbar = QToolBar()
        self.addToolBar(toolbar)

        self.page_spin = QSpinBox()
        self.page_spin.setRange(self.page_range.start, self.page_range.stop - 1)
        self.page_spin.setValue(self.current_page)
        self.page_spin.valueChanged.connect(self._on_page_spin_changed)
        toolbar.addWidget(QLabel(" 페이지 "))
        toolbar.addWidget(self.page_spin)

        act_prev = toolbar.addAction("◀ 이전")
        act_prev.setShortcut(Qt.Key.Key_Left)
        act_prev.triggered.connect(
            lambda: self.page_spin.setValue(max(self.page_range.start, self.current_page - 1))
        )
        act_next = toolbar.addAction("다음 ▶")
        act_next.setShortcut(Qt.Key.Key_Right)
        act_next.triggered.connect(
            lambda: self.page_spin.setValue(min(self.page_range.stop - 1, self.current_page + 1))
        )

        toolbar.addSeparator()
        self.type_combo = QComboBox()
        self.type_combo.addItems(BLOCK_TYPES)
        self.type_combo.activated.connect(lambda _: self._on_type_changed(self.type_combo.currentText()))
        toolbar.addWidget(QLabel(" 선택 블록 타입: "))
        toolbar.addWidget(self.type_combo)
        self.scene.selectionChanged.connect(self._sync_type_combo)

        act_merge = toolbar.addAction("병합")
        act_merge.setShortcut("Ctrl+M")
        act_merge.triggered.connect(self._merge_selected)

        act_delete = toolbar.addAction("삭제")
        act_delete.setShortcut(Qt.Key.Key_Delete)
        act_delete.triggered.connect(self._delete_selected)

        self.add_action = toolbar.addAction("새 블록 추가")
        self.add_action.setCheckable(True)
        self.add_action.setShortcut("N")
        self.add_action.toggled.connect(self._toggle_add_mode)

        toolbar.addSeparator()
        toolbar.addAction("다음 검토 필요 페이지").triggered.connect(self._jump_next_needs_review)
        act_save = toolbar.addAction("저장")
        act_save.setShortcut("S")
        act_save.triggered.connect(self._save)

        toolbar.addSeparator()
        self.done_action = toolbar.addAction("이 페이지 완료")
        self.done_action.setCheckable(True)
        self.done_action.setShortcut("D")
        self.done_action.toggled.connect(self._toggle_done)

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

        self.legend_action = toolbar.addAction("범례")
        self.legend_action.setCheckable(True)
        self.legend_action.setChecked(True)
        self.legend_action.setShortcut("L")
        self.legend_action.toggled.connect(self.legend.setVisible)

        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_label)

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
        for unit, page_number, block_count in page_plan:
            if progress.wasCanceled():
                cancelled = True
                break

            def on_progress(i: int, total: int, unit=unit, page_number=page_number) -> None:
                progress.setValue(done_blocks + i)
                pct = int((done_blocks + i) / total_blocks * 100)
                progress.setLabelText(f"{unit.title} — {page_number}쪽 블록 {i}/{total}  (전체 {pct}%)")
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
        if page_number not in self.pages_blocks:
            entries = pipeline.run_page(
                self.pdf_path,
                page_index=page_number - 1,
                page_number=page_number,
                dpi=150,
                backend=self.backend,
                cache=self.cache,
            )
            self.cache.save()
            self.pages_blocks[page_number] = entries
        flags = review.flag_needs_review([e["block"] for e in self.pages_blocks[page_number]])
        for e, f in zip(self.pages_blocks[page_number], flags):
            e["needs_review"] = f
        return self.pages_blocks[page_number]

    def _load_page(self, page_number: int) -> None:
        self.current_page = page_number
        img = segment.render_page(self.pdf_path, page_number - 1, 150)
        h, w = img.shape[:2]
        self._page_w, self._page_h = w, h
        self.pages_wh[page_number] = (w, h)

        self.scene.clear()
        pix_item = QGraphicsPixmapItem(_cv_to_qpixmap(img))
        pix_item.setZValue(-1)
        self.scene.addItem(pix_item)
        self.scene.setSceneRect(0, 0, w, h)

        for e in self._get_page_entries(page_number):
            item = BlockItem(e["block"], e["needs_review"], w, h)
            item.on_dirty = self._mark_dirty
            self.scene.addItem(item)

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
        entries = self.pages_blocks.get(self.current_page, [])
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

    def _merge_selected(self) -> None:
        items = self._selected_items()
        if len(items) < 2:
            return
        blocks = [it.block for it in items]
        x0 = min(b["bbox"][0] for b in blocks)
        y0 = min(b["bbox"][1] for b in blocks)
        x1 = max(b["bbox"][0] + b["bbox"][2] for b in blocks)
        y1 = max(b["bbox"][1] + b["bbox"][3] for b in blocks)
        biggest = max(blocks, key=lambda b: b["bbox"][2] * b["bbox"][3])

        entries = self.pages_blocks[self.current_page]
        for it in items:
            entries.remove(next(e for e in entries if e["block"] is it.block))
            self.scene.removeItem(it)

        new_block = {
            "id": f"p{self.current_page}_bm{len(entries):02d}",
            "page": self.current_page,
            "type": biggest["type"],
            "bbox": [x0, y0, x1 - x0, y1 - y0],
            "order": len(entries),
            "reflow": {"role": pipeline.ROLE_BY_TYPE.get(biggest["type"], "paragraph")},
        }
        entries.append({"block": new_block, "needs_review": False})
        item = BlockItem(new_block, False, self._page_w, self._page_h)
        item.on_dirty = self._mark_dirty
        self.scene.addItem(item)
        self._mark_dirty()
        self._update_status()

    def _delete_selected(self) -> None:
        entries = self.pages_blocks[self.current_page]
        items = self._selected_items()
        for it in items:
            entries.remove(next(e for e in entries if e["block"] is it.block))
            self.scene.removeItem(it)
        if items:
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
            "id": f"p{self.current_page}_bn{len(entries):02d}",
            "page": self.current_page,
            "type": "text",
            "bbox": [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h],
            "order": len(entries),
            "reflow": {"role": "paragraph"},
        }
        entries.append({"block": new_block, "needs_review": False})
        item = BlockItem(new_block, False, w, h)
        item.on_dirty = self._mark_dirty
        self.scene.addItem(item)
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
