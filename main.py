#!/usr/bin/env python3
"""HW GPU Monitor — Monitor de GPU estilo HWMonitor em Python + PySide6."""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from game_sessions import GameSessionTracker
from gpu_sensors import AmdGpuMonitor, FanStatus, GpuSnapshot, MetricStats


STYLESHEET = """
QMainWindow, QWidget {
    background-color: #14171d;
    color: #e6edf3;
    font-family: "Noto Sans", "Inter", "Segoe UI", sans-serif;
    font-size: 12px;
}

QFrame#HeaderCard, QFrame#SummaryCard, QFrame#ChartCard {
    background-color: #1c212b;
    border: 1px solid #2b3240;
    border-radius: 8px;
}

QFrame#GameBanner {
    background-color: #182738;
    border: 1px solid #2c4c70;
    border-radius: 7px;
}

QLabel#GameBannerText {
    font-size: 11px;
    font-weight: 600;
    color: #a5d6ff;
}

QLabel#GpuTitle {
    font-size: 13px;
    font-weight: 700;
    color: #ffffff;
}

QLabel#GpuSub {
    font-size: 10px;
    color: #8b949e;
}

QLabel#CardLabel {
    font-size: 10px;
    font-weight: 600;
    color: #8b949e;
    text-transform: uppercase;
}

QLabel#CardValue {
    font-size: 18px;
    font-weight: 700;
    color: #f0f6fc;
}

QLabel#CardSub {
    font-size: 10px;
    color: #9ba6b2;
}

QLabel#FanBadge {
    font-size: 11px;
    font-weight: 700;
    padding: 4px 9px;
    border-radius: 11px;
}

QTreeWidget {
    background-color: #1c212b;
    alternate-background-color: #181c24;
    border: 1px solid #2b3240;
    border-radius: 8px;
    padding: 2px;
    outline: none;
    font-size: 12px;
}

QTreeWidget::item {
    height: 21px;
    padding-left: 2px;
}

QTreeWidget::item:selected {
    background-color: #263248;
    color: #ffffff;
}

QHeaderView::section {
    background-color: #161a22;
    color: #9ba6b2;
    font-weight: 700;
    font-size: 11px;
    padding: 4px 6px;
    border: none;
    border-bottom: 1px solid #2b3240;
}

QPushButton {
    background-color: #263042;
    color: #e6edf3;
    border: 1px solid #38465f;
    border-radius: 6px;
    padding: 4px 10px;
    font-weight: 600;
    font-size: 11px;
}

QPushButton:hover {
    background-color: #314059;
}

QPushButton:pressed {
    background-color: #1f2836;
}

QComboBox {
    background-color: #1c212b;
    color: #e6edf3;
    border: 1px solid #38465f;
    border-radius: 5px;
    padding: 2px 6px;
    font-size: 11px;
}

QCheckBox {
    spacing: 4px;
    color: #c9d1d9;
    font-size: 11px;
}
"""

TEMP_ALERT_EDGE = 85.0
TEMP_ALERT_JUNCTION = 95.0
ALERT_COOLDOWN_SEC = 30.0


def _fmt(value: Optional[float], unit: str, decimals: int = 1) -> str:
    if value is None:
        return "—"
    if decimals == 0:
        return f"{value:.0f} {unit}"
    return f"{value:.{decimals}f} {unit}"


def _fmt_std(value: Optional[float], decimals: int = 1) -> str:
    if value is None:
        return "—"
    if decimals == 0:
        return f"±{value:.0f}"
    return f"±{value:.{decimals}f}"


def _temp_color(temp: Optional[float]) -> QColor:
    if temp is None:
        return QColor("#9ba6b2")
    if temp >= 85.0:
        return QColor("#ff6b6b")
    if temp >= 70.0:
        return QColor("#ffb84d")
    return QColor("#5ce1a8")


def _make_tray_icon(temp: Optional[float], fan_alert: bool) -> QIcon:
    """Desenha um ícone dinâmico com a temperatura atual para a bandeja do KDE."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    if fan_alert:
        bg = QColor("#c92a2a")
        fg = QColor("#ffffff")
    elif temp is not None and temp >= 85.0:
        bg = QColor("#d94848")
        fg = QColor("#ffffff")
    elif temp is not None and temp >= 70.0:
        bg = QColor("#d9822b")
        fg = QColor("#14171d")
    else:
        bg = QColor("#1f6f54")
        fg = QColor("#ffffff")

    painter.setBrush(QBrush(bg))
    painter.setPen(QPen(QColor("#0d1117"), 2))
    painter.drawRoundedRect(QRectF(3, 3, size - 6, size - 6), 14, 14)

    text = "!" if fan_alert else (f"{int(round(temp))}" if temp is not None else "--")
    font = QFont("Noto Sans", 24 if len(text) <= 2 else 18, QFont.Weight.Bold)
    painter.setFont(font)
    painter.setPen(fg)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()

    return QIcon(pixmap)


class TempChartWidget(QWidget):
    """Gráfico em tempo real de temperatura (GPU Edge + Hotspot) usando QPainter."""

    def __init__(self, max_points: int = 120) -> None:
        super().__init__()
        self._max_points = max_points
        self._history: Deque[Tuple[Optional[float], Optional[float]]] = deque(maxlen=max_points)
        self.setMinimumHeight(88)
        self.setMaximumHeight(105)

    def add_sample(self, edge: Optional[float], junction: Optional[float]) -> None:
        self._history.append((edge, junction))
        self.update()

    def clear(self) -> None:
        self._history.clear()
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        left, top, right, bottom = 32, 20, 8, 6
        plot = QRectF(left, top, max(10, rect.width() - left - right), max(10, rect.height() - top - bottom))

        # Escala vertical dinâmica (mínimo 20..100 °C)
        min_t, max_t = 20.0, 100.0
        for edge, junc in self._history:
            for v in (edge, junc):
                if v is not None and v > max_t - 5:
                    max_t = min(120.0, ((int(v) // 10) + 2) * 10.0)

        # Grade horizontal e rótulos °C
        grid_pen = QPen(QColor("#2b3240"), 1, Qt.PenStyle.DashLine)
        label_font = QFont("Noto Sans", 8)
        painter.setFont(label_font)

        for temp_mark in (20, 40, 60, 80, 100):
            if temp_mark > max_t:
                continue
            ratio = (temp_mark - min_t) / (max_t - min_t)
            y = plot.bottom() - ratio * plot.height()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor("#7d8590"))
            painter.drawText(QRectF(0, y - 8, left - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{temp_mark}°")

        # Legenda no topo
        last_edge, last_junc = self._history[-1] if self._history else (None, None)
        legend_font = QFont("Noto Sans", 9, QFont.Weight.DemiBold)
        painter.setFont(legend_font)

        painter.setPen(QColor("#5ce1a8"))
        painter.drawText(QPointF(plot.left(), 13), f"● GPU: {_fmt(last_edge, '°C', 1)}")

        painter.setPen(QColor("#ffb84d"))
        painter.drawText(QPointF(plot.left() + 115, 13), f"● Hotspot: {_fmt(last_junc, '°C', 1)}")

        painter.setPen(QColor("#7d8590"))
        painter.setFont(label_font)
        painter.drawText(
            QRectF(plot.left(), 2, plot.width(), 14),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{len(self._history)} amostras",
        )

        if len(self._history) < 2:
            painter.end()
            return

        def build_points(idx: int) -> list[QPointF]:
            pts: list[QPointF] = []
            count = len(self._history)
            step = plot.width() / max(1, self._max_points - 1)
            start_x = plot.right() - (count - 1) * step
            for i, sample in enumerate(self._history):
                val = sample[idx]
                if val is None:
                    continue
                clamped = max(min_t, min(max_t, val))
                ratio = (clamped - min_t) / (max_t - min_t)
                x = start_x + i * step
                y = plot.bottom() - ratio * plot.height()
                pts.append(QPointF(x, y))
            return pts

        # Curva GPU Edge + preenchimento suave
        edge_pts = build_points(0)
        if len(edge_pts) >= 2:
            line_path = QPainterPath(edge_pts[0])
            for pt in edge_pts[1:]:
                line_path.lineTo(pt)

            fill_path = QPainterPath(line_path)
            fill_path.lineTo(QPointF(edge_pts[-1].x(), plot.bottom()))
            fill_path.lineTo(QPointF(edge_pts[0].x(), plot.bottom()))
            fill_path.closeSubpath()

            grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            grad.setColorAt(0.0, QColor(92, 225, 168, 70))
            grad.setColorAt(1.0, QColor(92, 225, 168, 0))
            painter.fillPath(fill_path, QBrush(grad))

            painter.setPen(QPen(QColor("#5ce1a8"), 2.0))
            painter.drawPath(line_path)

        # Curva Hotspot
        junc_pts = build_points(1)
        if len(junc_pts) >= 2:
            junc_path = QPainterPath(junc_pts[0])
            for pt in junc_pts[1:]:
                junc_path.lineTo(pt)
            painter.setPen(QPen(QColor("#ffb84d"), 1.6))
            painter.drawPath(junc_path)

        painter.end()


class SummaryCard(QFrame):
    """Cartão de destaque rápido no topo da janela."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("SummaryCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(1)

        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName("CardLabel")

        self.value_lbl = QLabel("—")
        self.value_lbl.setObjectName("CardValue")

        self.sub_lbl = QLabel("—")
        self.sub_lbl.setObjectName("CardSub")

        layout.addWidget(self.title_lbl)
        layout.addWidget(self.value_lbl)
        layout.addWidget(self.sub_lbl)

    def set_data(self, value: str, sub: str, color: Optional[str] = None) -> None:
        self.value_lbl.setText(value)
        if color:
            self.value_lbl.setStyleSheet(f"color: {color};")
        else:
            self.value_lbl.setStyleSheet("")
        self.sub_lbl.setText(sub)


def _fmt_duration(seconds: int) -> str:
    mins, secs = divmod(max(0, seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m {secs:02d}s"


class HistoryDialog(QDialog):
    """Janela para visualizar o histórico de sessões de jogos da Steam."""

    def __init__(self, tracker: GameSessionTracker, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tracker = tracker
        self.setWindowTitle("Histórico de Jogos (Steam)")
        self.resize(780, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.info_lbl = QLabel(f"Arquivo: {self.tracker.history_path}")
        self.info_lbl.setObjectName("GpuSub")
        layout.addWidget(self.info_lbl)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["Data", "Jogo", "Duração", "GPU Méd", "GPU P95", "GPU ±σ", "GPU Máx", "Hotspot", "Fan Máx", "Clock Méd"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #1c212b; alternate-background-color: #181c24; "
            "border: 1px solid #2b3240; border-radius: 8px; gridline-color: #262c38; }"
        )

        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 106), (2, 62), (3, 64), (4, 64), (5, 58), (6, 64), (7, 64), (8, 72), (9, 72)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            hdr.resizeSection(col, width)
        layout.addWidget(self.table, stretch=1)

        btns = QHBoxLayout()
        self.copy_btn = QPushButton("Copiar JSON")
        self.copy_btn.clicked.connect(self._copy_json)
        self.clear_btn = QPushButton("Limpar")
        self.clear_btn.clicked.connect(self._clear_history)
        close_btn = QPushButton("Fechar")
        close_btn.clicked.connect(self.accept)

        btns.addWidget(self.copy_btn)
        btns.addWidget(self.clear_btn)
        btns.addStretch(1)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        self._load_rows()

    def _load_rows(self) -> None:
        records = self.tracker.load_history()
        self.table.setRowCount(len(records))
        for row_idx, rec in enumerate(records):
            started = str(rec.get("started_at", ""))[:16].replace("T", " ")
            game = str(rec.get("game", "—"))
            dur = _fmt_duration(int(rec.get("duration_sec", 0)))
            edge = rec.get("temp_edge_c", {})
            junc = rec.get("temp_junction_c", {})
            fan = rec.get("fan_rpm", {})
            clk = rec.get("clock_sclk_mhz", {})

            values = [
                started,
                game,
                dur,
                _fmt(edge.get("avg"), "°C", 1),
                _fmt(edge.get("p95"), "°C", 1),
                _fmt_std(edge.get("std_dev"), 1),
                _fmt(edge.get("max"), "°C", 1),
                _fmt(junc.get("max"), "°C", 1),
                _fmt(fan.get("max"), "RPM", 0),
                _fmt(clk.get("avg"), "MHz", 0),
            ]
            for col_idx, val in enumerate(values):
                cell = QTableWidgetItem(val)
                if col_idx != 1:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_idx, col_idx, cell)

    def _copy_json(self) -> None:
        records = self.tracker.load_history()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(json.dumps(records, indent=2, ensure_ascii=False))
        self.copy_btn.setText("Copiado!")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("Copiar JSON"))

    def _clear_history(self) -> None:
        self.tracker.clear_history()
        self._load_rows()


class GpuMonitorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.monitor = AmdGpuMonitor()
        self.session_tracker = GameSessionTracker()
        self._rows: Dict[str, tuple[QTreeWidgetItem, Callable[[Optional[float]], str], int]] = {}
        self._last_alert_ts: float = 0.0
        self._last_snapshot: Optional[GpuSnapshot] = None
        self._quitting: bool = False
        self._tray_hint_shown: bool = False
        self.tray_icon: Optional[QSystemTrayIcon] = None

        self.setWindowTitle("HW GPU Monitor")
        self.resize(650, 520)
        self.setMinimumSize(580, 440)

        self._build_ui()
        self._populate_tree()
        self._setup_tray()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Cabeçalho com nome da GPU + badge de status do Fan
        header = QFrame()
        header.setObjectName("HeaderCard")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 6, 10, 6)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        self.gpu_title = QLabel(self.monitor.gpu_name)
        self.gpu_title.setObjectName("GpuTitle")

        hwmon_info = self.monitor.hwmon_dir.name if self.monitor.hwmon_dir else "sem sensor"
        vram_info = (
            f" · {self.monitor.vram_total_mb / 1024:.0f} GB VRAM"
            if self.monitor.vram_total_mb
            else ""
        )
        self.gpu_sub = QLabel(f"amdgpu{vram_info} · {hwmon_info}")
        self.gpu_sub.setObjectName("GpuSub")

        title_box.addWidget(self.gpu_title)
        title_box.addWidget(self.gpu_sub)

        self.fan_badge = QLabel("FAN: —")
        self.fan_badge.setObjectName("FanBadge")
        self.fan_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        header_layout.addLayout(title_box, stretch=1)
        header_layout.addWidget(self.fan_badge)
        root.addWidget(header)

        # Banner discreto de sessão de jogo Steam (só aparece quando um jogo está rodando)
        self.game_banner = QFrame()
        self.game_banner.setObjectName("GameBanner")
        banner_layout = QHBoxLayout(self.game_banner)
        banner_layout.setContentsMargins(10, 4, 10, 4)
        self.game_banner_lbl = QLabel("")
        self.game_banner_lbl.setObjectName("GameBannerText")
        banner_layout.addWidget(self.game_banner_lbl)
        self.game_banner.setVisible(False)
        root.addWidget(self.game_banner)

        # Cartões de resumo rápido (Temperatura, Clock, Fan)
        cards_row = QHBoxLayout()
        cards_row.setSpacing(6)
        self.temp_card = SummaryCard("Temperatura GPU")
        self.clock_card = SummaryCard("Clock GPU")
        self.fan_card = SummaryCard("Ventoinha (Fan)")
        cards_row.addWidget(self.temp_card)
        cards_row.addWidget(self.clock_card)
        cards_row.addWidget(self.fan_card)
        root.addLayout(cards_row)

        # Gráfico de temperatura em tempo real
        chart_card = QFrame()
        chart_card.setObjectName("ChartCard")
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(6, 4, 6, 4)
        self.temp_chart = TempChartWidget(max_points=120)
        chart_layout.addWidget(self.temp_chart)
        root.addWidget(chart_card)

        # Tabela estilo HWMonitor (Sensor | Atual | Mín | Méd | P95 | ±σ | Máx)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(["Sensor", "Atual", "Mín", "Méd", "P95", "±σ", "Máx"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(14)

        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_item = self.tree.headerItem()
        if header_item is not None:
            header_item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            header_item.setToolTip(4, "Percentil 95: 95% das leituras ficaram abaixo deste valor")
            header_item.setToolTip(5, "Desvio padrão (±σ): mede a oscilação em torno da média")
        for col, width in ((1, 68), (2, 68), (3, 68), (4, 68), (5, 56), (6, 68)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            hdr.resizeSection(col, width)
            if header_item is not None:
                header_item.setTextAlignment(col, Qt.AlignmentFlag.AlignCenter)

        root.addWidget(self.tree, stretch=1)

        # Barra inferior de controles
        controls = QHBoxLayout()
        controls.setSpacing(6)

        self.interval_combo = QComboBox()
        self.interval_combo.addItem("500 ms", 500)
        self.interval_combo.addItem("1 s", 1000)
        self.interval_combo.addItem("2 s", 2000)
        self.interval_combo.setCurrentIndex(1)
        self.interval_combo.setToolTip("Intervalo de atualização")
        self.interval_combo.currentIndexChanged.connect(self._on_interval_changed)

        self.on_top_check = QCheckBox("Topo")
        self.on_top_check.setToolTip("Manter janela sempre no topo")
        self.on_top_check.toggled.connect(self._on_top_toggled)

        self.alerts_check = QCheckBox("Alertas")
        self.alerts_check.setChecked(True)
        self.alerts_check.setToolTip(
            f"Emite bip e notificação se a fan parar acima de 60 °C ou se a GPU passar de {TEMP_ALERT_EDGE:.0f} °C"
        )

        self.tray_close_check = QCheckBox("Bandeja")
        self.tray_close_check.setToolTip("Minimizar para a bandeja ao fechar a janela")
        self.tray_close_check.setChecked(QSystemTrayIcon.isSystemTrayAvailable())
        self.tray_close_check.setEnabled(QSystemTrayIcon.isSystemTrayAvailable())

        self.history_btn = QPushButton("Histórico")
        self.history_btn.setToolTip("Ver histórico de sessões de jogos da Steam")
        self.history_btn.clicked.connect(self._on_history_clicked)

        self.copy_json_btn = QPushButton("JSON")
        self.copy_json_btn.setToolTip("Copiar métricas atuais, mínimas, médias e máximas em JSON")
        self.copy_json_btn.clicked.connect(self._on_copy_json_clicked)

        self.reset_btn = QPushButton("Resetar")
        self.reset_btn.setToolTip("Resetar Mínima, Média e Máxima")
        self.reset_btn.clicked.connect(self._on_reset_clicked)

        controls.addWidget(self.interval_combo)
        controls.addWidget(self.on_top_check)
        controls.addWidget(self.alerts_check)
        controls.addWidget(self.tray_close_check)
        controls.addStretch(1)
        controls.addWidget(self.history_btn)
        controls.addWidget(self.copy_json_btn)
        controls.addWidget(self.reset_btn)
        root.addLayout(controls)

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(_make_tray_icon(None, False))
        self.tray_icon.setToolTip("HW GPU Monitor")

        menu = QMenu()
        toggle_action = QAction("Mostrar / Ocultar", self)
        toggle_action.triggered.connect(self._toggle_visibility)

        history_action = QAction("Histórico de Jogos", self)
        history_action.triggered.connect(self._on_history_clicked)

        copy_action = QAction("Copiar JSON", self)
        copy_action.triggered.connect(self._on_copy_json_clicked)

        reset_action = QAction("Resetar Mín / Máx", self)
        reset_action.triggered.connect(self._on_reset_clicked)

        quit_action = QAction("Sair", self)
        quit_action.triggered.connect(self._quit_app)

        menu.addAction(toggle_action)
        menu.addAction(history_action)
        menu.addAction(copy_action)
        menu.addAction(reset_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _toggle_visibility(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_visibility()

    def _quit_app(self) -> None:
        self._quitting = True
        self.session_tracker.finalize_if_active()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        if (
            not self._quitting
            and self.tray_icon is not None
            and self.tray_close_check.isChecked()
        ):
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray_icon.showMessage(
                    "HW GPU Monitor",
                    "Continuando em segundo plano na bandeja do sistema.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
            return
        super().closeEvent(event)
        if not self._quitting:
            self._quit_app()

    def _add_group(self, name: str) -> QTreeWidgetItem:
        group = QTreeWidgetItem(self.tree, [name, "", "", "", "", "", ""])
        font = QFont()
        font.setBold(True)
        group.setFont(0, font)
        group.setForeground(0, QColor("#79c0ff"))
        group.setExpanded(True)
        return group

    def _add_metric_row(
        self,
        parent: QTreeWidgetItem,
        key: str,
        label: str,
        formatter: Callable[[Optional[float]], str],
        std_decimals: int = 1,
    ) -> None:
        item = QTreeWidgetItem(parent, [label, "—", "—", "—", "—", "—", "—"])
        for col in (1, 2, 3, 4, 5, 6):
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignCenter)
        self._rows[key] = (item, formatter, std_decimals)

    def _populate_tree(self) -> None:
        g_temp = self._add_group("Temperaturas")
        self._add_metric_row(g_temp, "temp_edge", "GPU (Edge)", lambda v: _fmt(v, "°C", 1), 1)
        self._add_metric_row(g_temp, "temp_junction", "Hotspot (Junction)", lambda v: _fmt(v, "°C", 1), 1)
        self._add_metric_row(g_temp, "temp_mem", "Memória (VRAM)", lambda v: _fmt(v, "°C", 1), 1)

        g_clock = self._add_group("Clocks")
        self._add_metric_row(g_clock, "clock_sclk_mhz", "GPU Core (SCLK)", lambda v: _fmt(v, "MHz", 0), 0)
        self._add_metric_row(g_clock, "clock_mclk_mhz", "Memória (MCLK)", lambda v: _fmt(v, "MHz", 0), 0)

        g_fan = self._add_group("Ventoinha (Fan)")
        self._add_metric_row(g_fan, "fan_rpm", "Rotação (RPM)", lambda v: _fmt(v, "RPM", 0), 0)
        self._add_metric_row(g_fan, "fan_pwm_percent", "Velocidade (PWM)", lambda v: _fmt(v, "%", 0), 1)

        g_extra = self._add_group("Carga & Energia")
        self._add_metric_row(g_extra, "gpu_busy_percent", "Uso da GPU", lambda v: _fmt(v, "%", 0), 1)
        self._add_metric_row(g_extra, "vram_used_mb", "VRAM Usada", lambda v: _fmt(v, "MB", 0), 0)
        self._add_metric_row(g_extra, "power_watts", "Consumo", lambda v: _fmt(v, "W", 1), 1)

    def _apply_fan_badge(self, status: FanStatus) -> None:
        palette = {
            "ok": ("#133a2a", "#5ce1a8", "#236b4d"),
            "zero_rpm": ("#212936", "#9ecbff", "#38465f"),
            "alert": ("#4a191c", "#ff7b72", "#9e2a2b"),
            "unknown": ("#2b2f38", "#8b949e", "#444c56"),
        }
        bg, fg, border = palette.get(status.state, palette["unknown"])
        self.fan_badge.setText(status.label)
        self.fan_badge.setToolTip(status.detail)
        self.fan_badge.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border: 1px solid {border};"
        )

    def _check_alerts(self, snap: GpuSnapshot, fan_status: FanStatus) -> None:
        if not self.alerts_check.isChecked():
            return

        now = time.monotonic()
        if now - self._last_alert_ts < ALERT_COOLDOWN_SEC:
            return

        alert_title: Optional[str] = None
        alert_msg: Optional[str] = None

        if fan_status.state == "alert":
            alert_title = "ALERTA: Ventoinha da GPU parada!"
            alert_msg = fan_status.detail
        elif snap.temp_edge is not None and snap.temp_edge >= TEMP_ALERT_EDGE:
            alert_title = "ALERTA: Temperatura alta na GPU!"
            alert_msg = f"GPU em {snap.temp_edge:.1f} °C (Hotspot {_fmt(snap.temp_junction, '°C', 1)})"
        elif snap.temp_junction is not None and snap.temp_junction >= TEMP_ALERT_JUNCTION:
            alert_title = "ALERTA: Hotspot alto na GPU!"
            alert_msg = f"Hotspot em {snap.temp_junction:.1f} °C (GPU {_fmt(snap.temp_edge, '°C', 1)})"

        if alert_title and alert_msg:
            self._last_alert_ts = now
            QApplication.beep()
            if self.tray_icon is not None:
                self.tray_icon.showMessage(
                    alert_title,
                    alert_msg,
                    QSystemTrayIcon.MessageIcon.Critical,
                    5000,
                )

    def _update_tray(self, snap: GpuSnapshot, fan_status: FanStatus) -> None:
        if self.tray_icon is None:
            return
        icon = _make_tray_icon(snap.temp_edge, fan_status.state == "alert")
        self.tray_icon.setIcon(icon)
        self.setWindowIcon(icon)
        tooltip = (
            f"HW GPU Monitor\n"
            f"GPU: {_fmt(snap.temp_edge, '°C', 1)} | Hotspot: {_fmt(snap.temp_junction, '°C', 1)}\n"
            f"Clock: {_fmt(snap.clock_sclk_mhz, 'MHz', 0)} | Fan: {fan_status.detail}"
        )
        self.tray_icon.setToolTip(tooltip)

    def refresh(self) -> None:
        snap: GpuSnapshot = self.monitor.poll()
        self._last_snapshot = snap
        fan_status = self.monitor.evaluate_fan_status(snap)
        self._apply_fan_badge(fan_status)
        self.temp_chart.add_sample(snap.temp_edge, snap.temp_junction)

        # Atualiza cartões de resumo
        edge_stat: MetricStats = self.monitor.stats["temp_edge"]
        self.temp_card.set_data(
            _fmt(snap.temp_edge, "°C", 1),
            f"Méd {_fmt(edge_stat.avg_val, '°C', 0)} · P95 {_fmt(edge_stat.p95_val, '°C', 0)} · Máx {_fmt(edge_stat.max_val, '°C', 0)}",
            _temp_color(snap.temp_edge).name(),
        )

        sclk_stat = self.monitor.stats["clock_sclk_mhz"]
        self.clock_card.set_data(
            _fmt(snap.clock_sclk_mhz, "MHz", 0),
            f"P95 {_fmt(sclk_stat.p95_val, 'MHz', 0)} · Máx {_fmt(sclk_stat.max_val, 'MHz', 0)}",
            "#79c0ff",
        )

        fan_color = {
            "ok": "#5ce1a8",
            "zero_rpm": "#9ecbff",
            "alert": "#ff6b6b",
            "unknown": "#9ba6b2",
        }[fan_status.state]
        self.fan_card.set_data(
            _fmt(snap.fan_rpm, "RPM", 0),
            fan_status.detail,
            fan_color,
        )

        # Atualiza linhas da tabela HWMonitor
        for key, (item, formatter, std_dec) in self._rows.items():
            stat = self.monitor.stats[key]
            item.setText(1, formatter(stat.current))
            item.setText(2, formatter(stat.min_val))
            item.setText(3, formatter(stat.avg_val))
            item.setText(4, formatter(stat.p95_val))
            item.setText(5, _fmt_std(stat.std_dev, std_dec))
            item.setText(6, formatter(stat.max_val))

            if key.startswith("temp_"):
                item.setForeground(1, _temp_color(stat.current))
                item.setForeground(3, _temp_color(stat.avg_val))
                item.setForeground(4, _temp_color(stat.p95_val))
                item.setForeground(6, _temp_color(stat.max_val))

        self._update_game_session(snap)
        self._update_tray(snap, fan_status)
        self._check_alerts(snap, fan_status)

    def _update_game_session(self, snap: GpuSnapshot) -> None:
        event = self.session_tracker.update(snap)
        if event is not None and self.tray_icon is not None:
            kind, payload = event
            if kind == "saved" and isinstance(payload, dict):
                avg_t = _fmt(payload.get("temp_edge_c", {}).get("avg"), "°C", 1)
                max_t = _fmt(payload.get("temp_edge_c", {}).get("max"), "°C", 1)
                dur = _fmt_duration(int(payload.get("duration_sec", 0)))
                self.tray_icon.showMessage(
                    f"Sessão salva: {payload.get('game', 'Jogo')}",
                    f"Duração {dur} · GPU Méd {avg_t} · Máx {max_t}",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )

        active = self.session_tracker.active
        if active is not None:
            dur = _fmt_duration(active.duration_seconds())
            avg_t = _fmt(active.stats["temp_edge"].avg_val, "°C", 1)
            p95_t = _fmt(active.stats["temp_edge"].p95_val, "°C", 1)
            max_t = _fmt(active.stats["temp_edge"].max_val, "°C", 1)
            self.game_banner_lbl.setText(
                f"Steam · {active.game_name} · {dur} · GPU Méd {avg_t} · P95 {p95_t} · Máx {max_t}"
            )
            self.game_banner.setVisible(True)
        else:
            self.game_banner.setVisible(False)

    def _on_history_clicked(self) -> None:
        dlg = HistoryDialog(self.session_tracker, self)
        dlg.exec()

    def _on_interval_changed(self) -> None:
        ms = int(self.interval_combo.currentData())
        self.timer.setInterval(ms)

    def _on_top_toggled(self, checked: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.show()

    def _on_copy_json_clicked(self) -> None:
        payload = self.monitor.to_dict(self._last_snapshot)
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        self.copy_json_btn.setText("Copiado!")
        QTimer.singleShot(1500, lambda: self.copy_json_btn.setText("JSON"))

    def _on_reset_clicked(self) -> None:
        self.monitor.reset_min_max()
        self.refresh()


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLESHEET)
    window = GpuMonitorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
