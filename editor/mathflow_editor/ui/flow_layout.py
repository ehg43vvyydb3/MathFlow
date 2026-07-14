"""버튼이 다 안 들어가면 다음 줄로 넘기는 레이아웃 (Qt 공식 Flow Layout 예제 포팅).

QMainWindow.addToolBar()로 여러 QToolBar를 top 영역에 나란히 넣으면, 창이
좁아져도 줄바꿈되지 않고 창 자체의 최소 폭이 툴바 전체 합만큼 강제로 늘어나
버린다(직접 확인: PySide6 6.11에서 window.minimumSizeHint()가 툴바 합계 폭과
같아짐). 그래서 툴바 버튼들을 QToolBar 대신 이 FlowLayout을 쓰는 일반
QWidget에 담아 중앙 위젯 레이아웃 맨 위에 배치한다 — 폭이 부족해지면 다음
줄로 자연스럽게 넘어간다.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin: int = 0, h_spacing: int = 6, v_spacing: int = 6):
        super().__init__(parent)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items: list = []
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item) -> None:
        self._items.append(item)

    def horizontalSpacing(self) -> int:
        return self._h_spacing

    def verticalSpacing(self) -> int:
        return self._v_spacing

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y = effective.x(), effective.y()
        line_height = 0

        for item in self._items:
            item_w = item.sizeHint().width()
            item_h = item.sizeHint().height()
            next_x = x + item_w + self._h_spacing
            if next_x - self._h_spacing > effective.right() and line_height > 0:
                x = effective.x()
                y += line_height + self._v_spacing
                next_x = x + item_w + self._h_spacing
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(item_w, item_h)))

            x = next_x
            line_height = max(line_height, item_h)

        return y + line_height - rect.y() + margins.bottom()
