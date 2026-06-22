from __future__ import annotations

import csv
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "viewer_config.json"
DEFAULT_LATEST_DIR = APP_DIR / "data" / "latest"
REMOTE_FILES = [
    "status.json",
    "auction_board_flow.png",
    "auction_board_flow.csv",
    "auction_top5.png",
    "auction_top5.csv",
    "board_flow.png",
    "top5.png",
    "top5.csv",
]
HEADER_LABELS = {
    "成交额亿": "成交额",
    "近15m成交额亿": "近15m成交额",
    "5m成交额亿": "5m成交额",
    "1m成交额亿": "1m成交额",
}


def config_candidates() -> list[Path]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).resolve()
        candidates.append(exe_path.parent / "viewer_config.json")
        if len(exe_path.parents) >= 4:
            candidates.append(exe_path.parents[3] / "viewer_config.json")
    candidates.extend([Path.cwd() / "viewer_config.json", DEFAULT_CONFIG_PATH])
    return list(dict.fromkeys(candidates))


def load_config() -> dict:
    for path in config_candidates():
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    if getattr(sys, "frozen", False):
        return {
            "data_source": "http",
            "base_url": "http://192.168.1.5:8787/latest",
            "refresh_seconds": 10,
            "http_timeout_seconds": 5,
        }
    return {"latest_dir": str(DEFAULT_LATEST_DIR), "refresh_seconds": 10}


def resource_path(relative_path: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", APP_DIR)) / relative_path


class LatestDataSource:
    def __init__(self, config: dict) -> None:
        self.mode = str(config.get("data_source") or "").strip().lower()
        self.base_url = str(config.get("base_url") or "").strip().rstrip("/")
        if self.base_url and not self.mode:
            self.mode = "http"
        if self.mode != "http":
            self.mode = "local"
        if self.mode == "http":
            cache_name = str(config.get("cache_name") or "realtime_board_latest")
            self.latest_dir = Path(tempfile.gettempdir()) / cache_name
        else:
            self.latest_dir = Path(config.get("latest_dir") or DEFAULT_LATEST_DIR).expanduser().resolve()
        self.latest_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = float(config.get("http_timeout_seconds") or 5)
        self.last_status_raw: bytes | None = None
        self.last_error = ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def url_for(self, file_name: str) -> str:
        return f"{self.base_url}/{file_name}"

    def fetch_bytes(self, file_name: str) -> bytes:
        request = urllib.request.Request(self.url_for(file_name), headers={"Cache-Control": "no-cache"})
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.read()

    def atomic_write(self, file_name: str, payload: bytes) -> None:
        target = self.latest_dir / file_name
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(payload)
        tmp.replace(target)

    def sync(self) -> bool:
        if self.mode != "http":
            self.latest_dir.mkdir(parents=True, exist_ok=True)
            self.last_error = ""
            return True
        try:
            status_raw = self.fetch_bytes("status.json")
            changed = status_raw != self.last_status_raw
            missing = any(not (self.latest_dir / name).exists() for name in REMOTE_FILES)
            if changed or missing:
                self.atomic_write("status.json", status_raw)
                for file_name in REMOTE_FILES:
                    if file_name == "status.json":
                        continue
                    try:
                        self.atomic_write(file_name, self.fetch_bytes(file_name))
                    except urllib.error.HTTPError as exc:
                        if exc.code != 404:
                            raise
                self.last_status_raw = status_raw
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False


class ImagePanel(QWidget):
    def __init__(self, title: str, file_name: str, default_zoom: str | float = "fit") -> None:
        super().__init__()
        self.file_name = file_name
        self.default_zoom = default_zoom
        self.last_mtime: float | None = None
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-size: 14px; font-weight: 700; color: #202124;")
        self.image_label = QLabel("暂无图片")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.image_label.setStyleSheet("background: white; color: #777;")
        self.image_label.setMinimumSize(360, 260)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.original_pixmap: QPixmap | None = None
        self.zoom = 1.0

        self.zoom_label = QLabel("100%")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setMinimumWidth(52)
        zoom_out = QPushButton("-")
        zoom_in = QPushButton("+")
        actual_size = QPushButton("原图")
        fit_width = QPushButton("整图")
        zoom_out.clicked.connect(lambda: self.change_zoom(0.85))
        zoom_in.clicked.connect(lambda: self.change_zoom(1.18))
        actual_size.clicked.connect(lambda: self.set_zoom(1.0))
        fit_width.clicked.connect(self.fit_to_panel)

        controls = QHBoxLayout()
        controls.addWidget(self.title_label, 1)
        controls.addWidget(zoom_out)
        controls.addWidget(self.zoom_label)
        controls.addWidget(zoom_in)
        controls.addWidget(actual_size)
        controls.addWidget(fit_width)

        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(False)
        scroll.setWidget(self.image_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addLayout(controls)
        layout.addWidget(scroll, 1)

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.25, min(3.0, zoom))
        self.render_pixmap()

    def change_zoom(self, factor: float) -> None:
        self.set_zoom(self.zoom * factor)

    def fit_to_width(self) -> None:
        if not self.original_pixmap or self.original_pixmap.isNull():
            return
        width = max(320, self.scroll.viewport().width() - 16)
        self.set_zoom(width / self.original_pixmap.width())

    def fit_to_panel(self) -> None:
        if not self.original_pixmap or self.original_pixmap.isNull():
            return
        width = max(320, self.scroll.viewport().width() - 16)
        height = max(180, self.scroll.viewport().height() - 16)
        self.set_zoom(min(width / self.original_pixmap.width(), height / self.original_pixmap.height(), 1.0))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.default_zoom in {"fit", "contain"} and self.original_pixmap is not None:
            self.apply_default_zoom()
            self.render_pixmap(reset_scroll=False)

    def apply_default_zoom(self) -> None:
        if not self.original_pixmap or self.original_pixmap.isNull():
            return
        if isinstance(self.default_zoom, (float, int)):
            self.zoom = float(self.default_zoom)
        elif self.default_zoom == "contain":
            width = max(320, self.scroll.viewport().width() - 16)
            height = max(180, self.scroll.viewport().height() - 16)
            self.zoom = max(0.1, min(1.0, width / self.original_pixmap.width(), height / self.original_pixmap.height()))
        elif self.default_zoom == "fit":
            width = max(320, self.scroll.viewport().width() - 16)
            self.zoom = max(0.1, min(1.0, width / self.original_pixmap.width()))
        else:
            self.zoom = 1.0

    def render_pixmap(self, reset_scroll: bool = True) -> None:
        if not self.original_pixmap or self.original_pixmap.isNull():
            return
        logical_width = max(1, int(self.original_pixmap.width() * self.zoom))
        logical_height = max(1, int(self.original_pixmap.height() * self.zoom))
        dpr = self.devicePixelRatioF()
        physical_size = QSize(max(1, int(logical_width * dpr)), max(1, int(logical_height * dpr)))
        scaled = self.original_pixmap.scaled(
            physical_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        self.image_label.setPixmap(scaled)
        self.image_label.resize(logical_width, logical_height)
        self.zoom_label.setText(f"{int(self.zoom * 100)}%")
        if reset_scroll:
            self.scroll.horizontalScrollBar().setValue(0)
            self.scroll.verticalScrollBar().setValue(0)

    def load_image(self, latest_dir: Path) -> None:
        path = latest_dir / self.file_name
        if not path.exists():
            self.image_label.setText(f"未找到 {self.file_name}")
            return
        mtime = path.stat().st_mtime
        if self.last_mtime == mtime and self.original_pixmap is not None:
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.image_label.setText(f"无法读取 {path.name}")
            return
        self.original_pixmap = pixmap
        self.last_mtime = mtime
        self.apply_default_zoom()
        self.render_pixmap()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code) if ch.isdigit()).zfill(6)[-6:]


def is_20cm_code(code: str) -> bool:
    normalized = normalize_code(code)
    return normalized.startswith(("300", "301", "688", "689"))


def suffix_for_code(code: str) -> str:
    normalized = normalize_code(code)
    if normalized.startswith(("300", "301")):
        return "(创)"
    if normalized.startswith(("688", "689")):
        return "(科)"
    return ""


def pct_color(pct: float, code: str) -> tuple[QColor, QColor]:
    limit = 0.20 if is_20cm_code(code) else 0.10
    strength = pct / limit if limit else 0
    if strength >= 0.90:
        return QColor("#B42318"), QColor("#FFFFFF")
    if strength >= 0.80:
        return QColor("#F97066"), QColor("#7A271A")
    if strength >= 0.70:
        return QColor("#FECACA"), QColor("#991B1B")
    return QColor("#FEE4E2"), QColor("#A61B1B")


class Top5TablePanel(QWidget):
    def __init__(
        self,
        title: str,
        file_name: str,
        max_boards: int | None = None,
        board_source_file: str | None = None,
        board_source_value_col: str | None = None,
    ) -> None:
        super().__init__()
        self.file_name = file_name
        self.max_boards = max_boards
        self.board_source_file = board_source_file
        self.board_source_value_col = board_source_value_col
        self.last_mtime: float | None = None
        self.last_source_mtime: float | None = None
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-size: 14px; font-weight: 700; color: #202124;")
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(False)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setMinimumSectionSize(72)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setStyleSheet(
            "QTableWidget { background: white; gridline-color: #D0D5DD; font-size: 13px; }"
            "QHeaderView::section { background: #2F3645; color: white; font-size: 13px; font-weight: 700; padding: 5px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addWidget(self.title_label)
        layout.addWidget(self.table, 1)

    def format_value(self, column: str, value: str) -> str:
        if column == "涨幅":
            try:
                return f"{float(value) * 100:.2f}%"
            except Exception:
                return value
        if column in {"成交额亿", "近15m成交额亿", "5m成交额亿", "1m成交额亿", "竞价成交额亿", "量比"}:
            try:
                return f"{float(value):.2f}"
            except Exception:
                return value
        return value

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.apply_column_widths()

    def apply_column_widths(self) -> None:
        if self.table.columnCount() == 0:
            return
        columns = [
            self.table.horizontalHeaderItem(index).data(Qt.ItemDataRole.UserRole) or self.table.horizontalHeaderItem(index).text()
            for index in range(self.table.columnCount())
        ]
        weights = {
            "股票名称": 1.25,
            "股票代码": 1.0,
            "涨幅": 0.8,
            "成交额亿": 0.85,
            "近15m成交额亿": 1.25,
            "5m成交额亿": 1.05,
            "1m成交额亿": 1.05,
            "板块": 0.8,
        }
        total_weight = sum(weights.get(column, 1.0) for column in columns)
        width = max(720, self.table.viewport().width() - 2)
        assigned = 0
        for index, column in enumerate(columns):
            if index == len(columns) - 1:
                column_width = max(84, width - assigned)
            else:
                column_width = max(84, int(width * weights.get(column, 1.0) / total_weight))
                assigned += column_width
            self.table.setColumnWidth(index, column_width)

    def source_boards(self, latest_dir: Path) -> list[str]:
        if not self.board_source_file or self.max_boards is None:
            return []
        path = latest_dir / self.board_source_file
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            return []
        value_col = self.board_source_value_col
        if value_col and value_col in rows[0]:
            def key(row: dict[str, str]) -> float:
                try:
                    return float(row.get(value_col, 0) or 0)
                except Exception:
                    return 0.0

            rows = sorted(rows, key=key, reverse=True)
        boards: list[str] = []
        for row in rows:
            board = row.get("板块", "")
            if board and board not in boards:
                boards.append(board)
            if len(boards) >= self.max_boards:
                break
        return boards

    def load_table(self, latest_dir: Path) -> None:
        path = latest_dir / self.file_name
        if not path.exists():
            self.table.setRowCount(0)
            self.table.setColumnCount(1)
            self.table.setHorizontalHeaderLabels(["提示"])
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem(f"未找到 {self.file_name}"))
            return
        mtime = path.stat().st_mtime
        source_mtime = None
        if self.board_source_file:
            source_path = latest_dir / self.board_source_file
            source_mtime = source_path.stat().st_mtime if source_path.exists() else None
        if self.last_mtime == mtime and self.last_source_mtime == source_mtime:
            return
        self.last_mtime = mtime
        self.last_source_mtime = source_mtime
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            self.table.setRowCount(0)
            return
        columns = list(rows[0].keys())
        if self.max_boards is not None:
            allowed_boards = self.source_boards(latest_dir)
            if allowed_boards:
                rows = [row for row in rows if row.get("板块", "") in allowed_boards]
            else:
                boards: list[str] = []
                filtered = []
                for row in rows:
                    board = row.get("板块", "")
                    if board not in boards:
                        if len(boards) >= self.max_boards:
                            continue
                        boards.append(board)
                    if board in boards:
                        filtered.append(row)
                rows = filtered
        self.table.clear()
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels([HEADER_LABELS.get(column, column) for column in columns])
        for index, column in enumerate(columns):
            self.table.horizontalHeaderItem(index).setData(Qt.ItemDataRole.UserRole, column)
        self.table.setRowCount(len(rows))
        board_colors = ["#FFF7F7", "#FFF9E8", "#F3FAFF", "#F4FBF7", "#F8F5FF"]
        board_order = []
        for row in rows:
            board = row.get("板块", "")
            if board and board not in board_order:
                board_order.append(board)
        board_color = {board: QColor(board_colors[idx % len(board_colors)]) for idx, board in enumerate(board_order)}

        for row_idx, row in enumerate(rows):
            code = row.get("股票代码", "")
            twenty = is_20cm_code(code)
            for col_idx, column in enumerate(columns):
                value = row.get(column, "")
                if column == "股票名称":
                    value = f"{value}{suffix_for_code(code)}"
                item = QTableWidgetItem(self.format_value(column, value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                font = QFont()
                font.setPointSize(13)
                if twenty and column in {"股票名称", "股票代码"}:
                    font.setBold(True)
                    item.setForeground(QColor("#C1121F"))
                item.setFont(font)
                if column == "涨幅":
                    try:
                        fill, text = pct_color(float(row.get(column, 0)), code)
                        item.setBackground(fill)
                        item.setForeground(text)
                        font.setBold(True)
                        item.setFont(font)
                    except Exception:
                        pass
                else:
                    item.setBackground(board_color.get(row.get("板块", ""), QColor("#FFFFFF")))
                self.table.setItem(row_idx, col_idx, item)
        self.table.resizeRowsToContents()
        for row_idx in range(self.table.rowCount()):
            self.table.setRowHeight(row_idx, 29)
        self.apply_column_widths()


class ViewerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        config = load_config()
        self.data_source = LatestDataSource(config)
        self.latest_dir = self.data_source.latest_dir
        self.refresh_seconds = int(config.get("refresh_seconds") or 10)

        self.setWindowTitle("实时资金流看板")
        icon_path = resource_path("assets/sinowise_logo.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1480, 920)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #444;")
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh)
        self.auction_toggle = QPushButton("隐藏竞价")
        self.auction_toggle.setCheckable(True)
        self.auction_toggle.setChecked(True)
        self.auction_toggle.clicked.connect(self.toggle_auction)

        self.interval = QComboBox()
        for value in [5, 10, 15, 30, 60]:
            self.interval.addItem(f"{value}s", value)
        index = self.interval.findData(self.refresh_seconds)
        if index >= 0:
            self.interval.setCurrentIndex(index)
        self.interval.currentIndexChanged.connect(self.change_interval)

        topbar = QHBoxLayout()
        topbar.addWidget(self.status, 1)
        topbar.addWidget(self.interval)
        topbar.addWidget(self.auction_toggle)
        topbar.addWidget(refresh_button)

        self.left_panel = ImagePanel("板块资金流", "board_flow.png", default_zoom=0.28)
        self.right_panel = Top5TablePanel("Top5 强势股", "top5.csv")
        self.top3_panel = Top5TablePanel(
            "Top3 竞价强势股",
            "auction_top5.csv",
            max_boards=3,
            board_source_file="auction_board_flow.csv",
            board_source_value_col="竞价净额亿",
        )
        self.auction_panel = ImagePanel("竞价资金流", "auction_board_flow.png", default_zoom="contain")
        self.auction_panel.setMinimumHeight(520)
        self.top3_panel.setMinimumHeight(520)
        self.left_panel.setMinimumHeight(470)
        self.right_panel.setMinimumHeight(470)

        auction_splitter = QSplitter(Qt.Orientation.Horizontal)
        auction_splitter.addWidget(self.auction_panel)
        auction_splitter.addWidget(self.top3_panel)
        auction_splitter.setSizes([680, 820])
        self.auction_splitter = auction_splitter

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.left_panel)
        splitter.addWidget(self.right_panel)
        splitter.setSizes([640, 860])
        self.main_splitter = splitter

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(6, 4, 6, 6)
        main_layout.setSpacing(4)
        main_layout.addLayout(topbar)
        main_layout.addWidget(auction_splitter, 6)
        main_layout.addWidget(splitter, 9)

        root = QWidget()
        root.setLayout(main_layout)
        self.root_widget = root
        self.apply_layout_mode()
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setWidget(root)
        self.setCentralWidget(page)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(self.refresh_seconds * 1000)
        self.refresh()

    def toggle_auction(self) -> None:
        visible = self.auction_toggle.isChecked()
        self.auction_toggle.setText("隐藏竞价" if visible else "显示竞价")
        self.apply_layout_mode()

    def apply_layout_mode(self) -> None:
        if self.auction_toggle.isChecked():
            self.root_widget.setMinimumHeight(1040)
            self.auction_splitter.setVisible(True)
            self.auction_panel.setVisible(True)
            self.top3_panel.setVisible(True)
            self.left_panel.setMinimumHeight(470)
            self.right_panel.setMinimumHeight(470)
        else:
            self.root_widget.setMinimumHeight(680)
            self.auction_splitter.setVisible(False)
            self.auction_panel.setVisible(False)
            self.top3_panel.setVisible(False)
            self.left_panel.setMinimumHeight(620)
            self.right_panel.setMinimumHeight(620)

    def change_interval(self) -> None:
        self.refresh_seconds = int(self.interval.currentData())
        self.timer.start(self.refresh_seconds * 1000)
        self.refresh()

    def read_status(self) -> dict:
        path = self.latest_dir / "status.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def refresh(self) -> None:
        synced = self.data_source.sync()
        self.latest_dir = self.data_source.latest_dir
        self.auction_panel.load_image(self.latest_dir)
        self.top3_panel.load_table(self.latest_dir)
        self.left_panel.load_image(self.latest_dir)
        self.right_panel.load_table(self.latest_dir)
        status = self.read_status()
        date_text = (
            str(status.get("latest_main_time") or status.get("latest_auction_time") or status.get("status_updated_at") or "")[:10]
            or "未知"
        )
        error = status.get("last_error") or ""
        error_text = f" | 错误：{error}" if error else ""
        mode_map = {
            "waiting": "等待",
            "auction_running": "竞价更新中",
            "main_running": "盘中更新中",
            "auction": "竞价完成",
            "main_flow": "盘中完成",
            "auction_error": "竞价失败",
            "main_error": "盘中失败",
        }
        mode_text = mode_map.get(str(status.get("mode") or ""), str(status.get("mode") or ""))
        next_run = str(status.get("next_run") or "")
        next_label = str(status.get("next_label") or status.get("next_kind") or "")
        next_text = f" | 下次：{next_run[11:16]} {next_label}" if next_run else ""
        mode_part = f" | 状态：{mode_text}" if mode_text else ""
        source_text = " | 来源：远程" if self.data_source.mode == "http" else ""
        sync_error = f" | 连接失败：{self.data_source.last_error}" if not synced and self.data_source.last_error else ""
        self.status.setText(f"当前日期：{date_text}{mode_part}{next_text}{source_text}{error_text}{sync_error}")


def main() -> None:
    app = QApplication(sys.argv)
    icon_path = resource_path("assets/sinowise_logo.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = ViewerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
