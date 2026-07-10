"""블록 검토 화면 진입점.

사용법: editor/.venv에서 `python -m mathflow_editor.ui.main`
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .review_window import ReviewWindow

PDF_PATH = Path.home() / "Downloads" / "공통수학2.pdf"
BOOK_ID = "gongtong-math-2"
PAGE_RANGE = range(10, 34)  # "1. 평면좌표" 단원


def main() -> None:
    app = QApplication(sys.argv)
    window = ReviewWindow(PDF_PATH, book_id=BOOK_ID, page_range=PAGE_RANGE)
    window.resize(1400, 1000)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
