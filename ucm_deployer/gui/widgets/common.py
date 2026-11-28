# -*- coding: utf-8 -*-
"""GUI 通用组件：任务线程（Python threading + Qt 信号投递）、并行任务面板、
镜像下拉（关键字筛选）、远端目录浏览。

任务线程用 Python threading.Thread 而非 QThread 承载：paramiko 等纯 Python
库在 Qt C++ 线程中运行存在清理竞态（线程结束时偶发 access violation），
Python 线程由解释器统一管理更稳妥；信号经 QObject 容器跨线程安全投递。
"""
from __future__ import annotations

import os
import threading
import weakref
from typing import Callable, ClassVar, Dict, List, Optional, Tuple

from PySide6.QtCore import QDateTime, QEvent, QObject, QStandardPaths, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.models import DockerImage, ServerInfo
from ...core.ssh_client import SSHClient

# 运行中的任务线程（弱引用；测试收尾时统一 join）
_task_threads: "weakref.WeakSet[threading.Thread]" = weakref.WeakSet()


# ============================================================ 镜像下拉
def fill_image_combo(combo: QComboBox, images: List[DockerImage],
                     current: str = "") -> None:
    """填充镜像下拉框：只可选择（不可手输），显示镜像大小，userData 存 ref。

    - 疑似 UCM 镜像优先排序；悬浮提示显示创建时间
    - 优先恢复 current（上次选择/已记录值），否则选第一项
    - 无镜像时显示占位提示（data 为空串，视作未选择）
    """
    combo.blockSignals(True)
    prev = current or combo_ref(combo)
    combo.clear()
    if not images:
        combo.addItem("（尚未加载镜像，请点击「刷新镜像列表」）")
        combo.setItemData(0, "", Qt.UserRole)
        combo.blockSignals(False)
        return
    ordered = sorted(images, key=lambda im: "ucm" not in im.ref.lower())
    for im in ordered:
        label = f"{im.ref}    {im.size}".rstrip()
        combo.addItem(label)
        idx = combo.count() - 1
        combo.setItemData(idx, im.ref, Qt.UserRole)
        if im.created:
            combo.setItemData(idx, f"创建时间: {im.created}", Qt.ToolTipRole)
    for i in range(combo.count()):
        if str(combo.itemData(i, Qt.UserRole) or "") == prev and prev:
            combo.setCurrentIndex(i)
            break
    else:
        combo.setCurrentIndex(0)
    combo.blockSignals(False)


def combo_ref(combo: QComboBox) -> str:
    """取镜像下拉当前选择的镜像引用（userData），未选择返回空串。"""
    if combo.count() <= 0:
        return ""
    return str(combo.currentData(Qt.UserRole) or "")


def image_from_ref(ref: str) -> DockerImage:
    """由 ref 构造占位 DockerImage（用于预填/新构建的镜像，无大小信息）。"""
    return DockerImage(repository=ref, tag="", image_id=ref)


def setup_image_combo(combo: QComboBox, max_visible: int = 10) -> None:
    """配置镜像下拉框：弹层最多显示 max_visible 条（超出滚动），输入关键字即可筛选。

    仍只能选中列表中的镜像（不可自由输入）：NoInsert 禁止新增项；
    结束编辑（回车/失焦）时文本未匹配任何列表项则回退当前选择。
    """
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    combo.setMaxVisibleItems(max_visible)
    edit = combo.lineEdit()
    edit.setPlaceholderText("输入关键字筛选镜像")

    completer = QCompleter(combo.model(), combo)
    completer.setCompletionMode(QCompleter.PopupCompletion)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setFilterMode(Qt.MatchContains)
    completer.setMaxVisibleItems(max_visible)
    completer.popup().setObjectName("imageFilterPopup")
    combo.setCompleter(completer)

    def commit() -> None:
        text = edit.text().strip()
        idx = combo.findText(text, Qt.MatchContains) if text else -1
        if idx >= 0:
            combo.setCurrentIndex(idx)
        elif combo.count() > 0:
            combo.setCurrentIndex(max(combo.currentIndex(), 0))
            edit.setText(combo.currentText())

    def pick(text: str) -> None:
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    completer.activated.connect(pick)
    edit.editingFinished.connect(commit)


# ============================================================ 任务线程
class TaskSignals(QObject):
    """任务线程 -> GUI 线程的信号（跨线程 emit 自动队列投递）。"""

    sig_progress = Signal(str, int, str)   # server_id, pct, msg
    sig_log = Signal(str, str)             # server_id, line
    sig_done = Signal(str, bool, str)      # server_id, ok, message


class QuickSignals(QObject):
    sig_done = Signal(object, object)      # result, error


class TaskContext:
    """任务函数使用的回调上下集（线程安全：通过 Qt 信号投递到 GUI 线程）。"""

    def __init__(self, thread: "ServerTaskThread"):
        self._thread = thread

    def progress(self, pct: int, msg: str = "") -> None:
        self._thread.signals.sig_progress.emit(self._thread.server.id, int(pct), msg)

    def log(self, line: str) -> None:
        self._thread.signals.sig_log.emit(self._thread.server.id, str(line))

    def cancelled(self) -> bool:
        return self._thread.cancel_requested


class ServerTaskThread(threading.Thread):
    """绑定一台服务器的后台任务线程。"""

    def __init__(self, server: ServerInfo, fn: Callable,
                 parent: Optional[QObject] = None):
        super().__init__(daemon=True, name=f"task-{server.name}")
        self.server = server
        self.fn = fn
        self.signals = TaskSignals(parent)
        self.cancel_requested = False

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def run(self) -> None:  # pragma: no cover - 线程体
        ctx = TaskContext(self)
        try:
            with SSHClient(self.server) as ssh:
                result = self.fn(ssh, ctx)
            # 任务函数可返回状态消息（str）作为完成提示；否则用通用的「完成」
            message = (result if isinstance(result, str) and result.strip()
                       else "完成")
            self.signals.sig_done.emit(self.server.id, True, message)
        except Exception as exc:
            self.signals.sig_done.emit(self.server.id, False, str(exc))


class QuickTask(threading.Thread):
    """单次后台操作（不显示面板）。done(result, error)"""

    def __init__(self, fn: Callable, parent: Optional[QObject] = None):
        super().__init__(daemon=True, name="quick-task")
        self.fn = fn
        self.signals = QuickSignals(parent)

    def run(self) -> None:  # pragma: no cover
        try:
            self.signals.sig_done.emit(self.fn(), None)
        except Exception as exc:
            self.signals.sig_done.emit(None, exc)


# ============================================================ 并行任务面板
class ParallelTaskPanel(QWidget):
    """任务执行面板：一行进度条 + 一页日志，支持取消。

    用法（推荐）：
        panel.run_tasks(tasks, "标题", on_finished=lambda all_ok: ...)
    on_finished 由面板内部一次性连接，任务被拒（忙碌）时不会残留。

    同一服务器可以同时有多个任务（如批量部署中源服务器既准备镜像包、
    又作为部署目标）：进度条/日志/结果按「任务线程」而非 server.id
    路由，避免互相覆盖。
    """

    finished_all = Signal(bool)

    # shutdown 超时未结束的线程脱离托管（不能 terminate：曾引发进程级 abort）
    _ORPHANS: "ClassVar[set]" = set()

    def __init__(self, parent=None, split_layout: bool = False):
        super().__init__(parent)
        self._threads: List[ServerTaskThread] = []
        self._results: Dict[ServerTaskThread, Tuple[bool, str]] = {}
        self._total = 0
        self._logs: Dict[ServerTaskThread, QPlainTextEdit] = {}
        self._bars: Dict[ServerTaskThread, QProgressBar] = {}

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["服务器", "进度", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        # 常驻占位日志页：无任务时填满面板下方空白，任务启动后由任务页签替换。
        # Ignored 尺寸策略：不抬高 tabs 的最小高度（否则会顶爆一屏排版）。
        self._placeholder_log = QLabel(
            "暂无任务日志。执行操作（扫描镜像 / 构建 / 创建容器 / 预检等）后，\n"
            "任务的过程与结果日志会显示在此处，可随时点击「⬇ 导出日志」保存。")
        self._placeholder_log.setAlignment(Qt.AlignCenter)
        self._placeholder_log.setWordWrap(True)
        self._placeholder_log.setProperty("role", "hint")
        self._placeholder_log.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.tabs.addTab(self._placeholder_log, "日志")
        self.tabs.setMinimumHeight(76)   # 保证任务面板有可用高度（不被压缩过扁）
        # 无任务初始态隐藏页签头：占位提示直接展示（「日志」二字只在数据层保留）
        self.tabs.tabBar().setVisible(False)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        self.export_btn = QPushButton("⬇ 导出日志")
        self.export_btn.setToolTip(
            "将当前页签的任务日志导出为 .log 文件（含原始输出，便于定位问题）")
        self.export_btn.clicked.connect(self._export_log)
        self.title_label = QLabel("任务记录")
        self.title_label.setStyleSheet("color:#64748b;")
        # 紧凑模式（单任务）：单行进度条 + 状态文字，隐藏表格与页签栏
        self.compact_bar = QProgressBar()
        self.compact_bar.setRange(0, 100)
        self.compact_bar.setValue(0)
        self.compact_bar.setFormat("等待中")
        self.compact_status = QLabel("")
        self.compact_status.setStyleSheet("color:#64748b;")
        top = QHBoxLayout()
        top.addWidget(self.title_label)
        top.addWidget(self.compact_bar, 1)
        top.addWidget(self.compact_status)
        top.addStretch(1)
        top.addWidget(self.export_btn)
        top.addWidget(self.cancel_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        if split_layout:
            # 拆分布局：结果表留在本面板，日志页签由外部容器负责布局
            # （如步骤1 的 4:3:3 三段分栏）；_apply_mode 对两者的可见性
            # 控制不受父容器变化影响
            layout.addWidget(self.table)
            return
        layout.addWidget(self.table)
        layout.addWidget(self.tabs, 1)

        # 无任务初始态：隐藏空表格（0 行）与"等待中"进度条，避免大片无意义空白；
        # start() 时按任务数由 _apply_mode 设置显示模式
        self.table.setVisible(False)
        self.compact_bar.setVisible(False)
        self.compact_status.setVisible(False)

    def _apply_mode(self, compact: bool) -> None:
        """单任务紧凑 / 多任务完整两种呈现；数据结构不变。"""
        self.table.setVisible(not compact)
        self.tabs.tabBar().setVisible(not compact)
        self.compact_bar.setVisible(compact)
        self.compact_status.setVisible(compact)

    # ------------------------------------------------------------ API
    def is_running(self) -> bool:
        return self._total > len(self._results)

    def start(self, tasks: List[Tuple[ServerInfo, Callable]],
              title: str = "执行任务") -> bool:
        """启动任务；忙碌时提示并返回 False。"""
        if self.is_running():
            QMessageBox.warning(self, "忙碌", "有任务正在执行，请等待完成或取消")
            return False
        self._apply_mode(len(tasks) == 1)
        self._threads = []
        self._results = {}
        self._total = len(tasks)
        self._logs = {}
        self._bars = {}
        self.title_label.setText(title if title else "")
        self.compact_bar.setValue(0)
        self.compact_bar.setFormat("等待中")
        self.compact_status.setStyleSheet("color:#64748b;")
        self.compact_status.setText("排队中" if tasks else "")
        self.table.setRowCount(0)
        while self.tabs.count():
            self.tabs.removeTab(0)

        for server, fn in tasks:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(f"{server.name} ({server.host})"))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("等待中")
            self.table.setCellWidget(row, 1, bar)
            status = QTableWidgetItem("排队中")
            status.setForeground(Qt.gray)
            self.table.setItem(row, 2, status)
            edit = QPlainTextEdit()
            edit.setReadOnly(True)
            edit.setMaximumBlockCount(5000)
            edit.setProperty("role", "code")
            font = QFont("Consolas")
            font.setStyleHint(QFont.Monospace)
            edit.setFont(font)
            self.tabs.addTab(edit, server.name)

            thread = ServerTaskThread(server, fn, self)
            self._bars[thread] = bar
            self._logs[thread] = edit
            # 按「线程」路由信号（同一服务器多任务时行/页签互不覆盖）
            thread.signals.sig_progress.connect(
                lambda _sid, pct, msg, t=thread: self._on_progress(t, pct, msg))
            thread.signals.sig_log.connect(
                lambda _sid, line, t=thread: self._on_log(t, line))
            thread.signals.sig_done.connect(
                lambda _sid, ok, message, t=thread: self._on_done(t, ok, message))
            self._threads.append(thread)
            _task_threads.add(thread)

        self.cancel_btn.setEnabled(bool(tasks))
        for t in self._threads:
            t.start()
        return True

    def run_tasks(self, tasks: List[Tuple[ServerInfo, Callable]],
                  title: str = "执行任务",
                  on_finished: Optional[Callable[[bool], None]] = None) -> bool:
        """start() + 一次性 on_finished 回调（面板内部管理连接，无残留）。"""
        if self.is_running():
            QMessageBox.warning(self, "忙碌", "有任务正在执行，请等待完成或取消")
            return False

        def handler(all_ok: bool) -> None:
            # 先断开自身再回调：on_finished 里可能弹模态框或启动新任务，
            # 期间 finished_all 若再次发射，旧回调不能被重入
            # （曾导致「构建完成」提示连环弹出）
            try:
                self.finished_all.disconnect(handler)
            except (RuntimeError, TypeError):
                pass
            if on_finished is not None:
                on_finished(all_ok)

        if on_finished is not None:
            self.finished_all.connect(handler)
        return self.start(tasks, title)

    def shutdown(self, timeout_s: float = 5.0) -> None:
        """请求取消并等待线程结束（关闭窗口时使用，避免遗留线程）。"""
        for t in self._threads:
            t.request_cancel()
        for t in self._threads:
            t.join(timeout=timeout_s)
            if t.is_alive():
                # 不 terminate（曾引发进程级 abort）：脱离托管，任其自然结束
                self._ORPHANS.add(t)

    # ------------------------------------------------------------ 内部
    def _cancel(self) -> None:
        for t in self._threads:
            t.request_cancel()
        self.cancel_btn.setEnabled(False)
        self._set_status_all("正在取消...")

    def _export_log(self) -> None:
        """导出当前页签的任务日志到本地文件（UTF-8，便于存档/定位问题）。"""
        from PySide6.QtWidgets import QFileDialog

        edit = self.tabs.currentWidget()
        text = edit.toPlainText() if isinstance(edit, QPlainTextEdit) else ""
        if not text.strip():
            QMessageBox.information(self, "提示", "当前任务暂无日志内容")
            return
        title = (self.title_label.text() or "任务").strip()
        safe = "".join(c for c in title if c not in '\\/:*?"<>|').strip() or "任务"
        stamp = QDateTime.currentDateTime().toString("yyyyMMdd-HHmmss")
        default_dir = QStandardPaths.writableLocation(
            QStandardPaths.DownloadLocation) or ""
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志",
            os.path.join(default_dir, f"ucm-deployer-{safe}-{stamp}.log"),
            "日志文件 (*.log *.txt);;所有文件 (*)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        QMessageBox.information(self, "完成", f"日志已导出:\n{path}")

    def _row_of(self, thread: ServerTaskThread) -> int:
        for i, t in enumerate(self._threads):
            if t is thread:
                return i
        return -1

    def _on_progress(self, thread: ServerTaskThread, pct: int, msg: str) -> None:
        bar = self._bars.get(thread)
        if bar is not None:
            bar.setValue(max(0, min(100, int(pct))))
            # 进度条只显示百分比；具体信息进入状态列，避免两处重复
            bar.setFormat(f"{pct}%")
            if msg:
                row = self._row_of(thread)
                item = self.table.item(row, 2) if row >= 0 else None
                if item is not None:
                    item.setText(msg)
                    item.setForeground(Qt.darkGray)
        if self.compact_bar.isVisible():
            self.compact_bar.setValue(max(0, min(100, int(pct))))
            self.compact_bar.setFormat(f"{pct}%")
            if msg:
                self.compact_status.setText(msg)

    def _on_log(self, thread: ServerTaskThread, line: str) -> None:
        edit = self._logs.get(thread)
        if edit is not None:
            edit.appendPlainText(line)

    def _set_status_all(self, text: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 2)
            if item is not None:
                item.setText(text)

    def _on_done(self, thread: ServerTaskThread, ok: bool, message: str) -> None:
        self._results[thread] = (ok, message)
        row = self._row_of(thread)
        bar = self._bars.get(thread)
        if bar is not None:
            if ok:
                bar.setValue(100)
                bar.setFormat("100%")
            else:
                bar.setFormat("失败")
        if row >= 0:
            item = self.table.item(row, 2)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, 2, item)
            item.setText(("✓ " if ok else "✗ ") + message)
            item.setForeground(Qt.darkGreen if ok else Qt.red)
        if self.compact_bar.isVisible():
            if ok:
                self.compact_bar.setValue(100)
                self.compact_bar.setFormat("100%")
            else:
                self.compact_bar.setFormat("失败")
            self.compact_status.setText(("✓ " if ok else "✗ ") + message)
            self.compact_status.setStyleSheet(
                "color:#16a34a;" if ok else "color:#dc2626;")
        if self._total and len(self._results) >= self._total:
            self.cancel_btn.setEnabled(False)
            all_ok = all(ok for ok, _ in self._results.values())
            self.finished_all.emit(all_ok)


# ============================================================ 远端目录选择
class RemoteDirDialog(QDialog):
    """浏览远端服务器目录。lister(path) -> [子目录列表]。"""

    def __init__(self, parent, lister: Callable[[str], List[str]],
                 start: str = "/", title: str = "选择服务器目录"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(540, 440)
        self._lister = lister
        self._selected: Optional[str] = None

        self.path_edit = QLineEdit(start)
        up_btn = QPushButton("↑ 上级")
        refresh_btn = QPushButton("刷新")
        up_btn.clicked.connect(self._up)
        refresh_btn.clicked.connect(self._load)

        self.listw = QListWidget()
        self.listw.itemDoubleClicked.connect(self._enter)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(self.path_edit, 1)
        top.addWidget(up_btn)
        top.addWidget(refresh_btn)
        layout.addLayout(top)
        layout.addWidget(self.listw, 1)
        layout.addWidget(QLabel("双击进入子目录；「确定」使用当前路径"))
        layout.addWidget(self.buttons)
        self._load()

    def _load(self) -> None:
        path = self.path_edit.text().strip() or "/"
        self.listw.clear()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            dirs = self._lister(path)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "读取失败", f"无法列出 {path}:\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        for d in dirs:
            self.listw.addItem(QListWidgetItem("📁 " + d))

    def _enter(self, item: QListWidgetItem) -> None:
        text = item.text()
        if text.startswith("📁 "):
            self.path_edit.setText(text[2:])
            self._load()

    def _up(self) -> None:
        path = self.path_edit.text().strip().rstrip("/")
        if path.startswith("/") and "/" in path[1:]:
            self.path_edit.setText(path.rsplit("/", 1)[0] or "/")
        else:
            self.path_edit.setText("/")
        self._load()

    def _accept(self) -> None:
        self._selected = self.path_edit.text().strip() or "/"
        self.accept()

    def selected_path(self) -> Optional[str]:
        return self._selected


# ============================================================ 远端文件系统挂载
class RemoteMountDialog(QDialog):
    """在宿主机上挂载远端文件系统（NFS over RDMA / DTFS）。

    - NFS over RDMA：mount -t nfs -o vers=3,nolock,proto=rdma,nconnect=8 <IP>:/<fs> <dir>
    - DTFS：mount -t dtfs -o rw,eventual,cid=<CID>,dn=dns_local /<fs> <dir>

    本地挂载目录默认 /mnt/<fs 名>（不存在时由执行方 mkdir -p 创建）；
    命令实时预览；点「挂载」时发射 mount_requested 信号，由调用方
    （步骤3 / 批量部署页面）通过 SSH 执行并在成功后把目录加入挂载表。
    """

    mount_requested = Signal()

    def __init__(self, parent, title: str = "新增远端挂载",
                 ok_text: str = "💾 挂载") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(600)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["NFS over RDMA", "DTFS"])

        self.peer_label = QLabel("远端 IP")
        self.peer_edit = QLineEdit()
        self.peer_edit.setPlaceholderText("NFS 服务器地址（如 192.168.1.10）")

        self.fs_edit = QLineEdit("fs1")
        self.fs_edit.setToolTip("远端文件系统名，如 fs1；命令中显示为 /fs1")

        self.dir_edit = QLineEdit("/mnt/fs1")
        self.dir_edit.setToolTip("宿主机本地挂载目录；默认 /mnt/<fs 名>，"
                                 "不存在时执行挂载前会自动创建")
        self._dir_dirty = False      # 用户手动改过目录后不再自动跟随 fs 名

        self.table_type_combo = QComboBox()
        self.table_type_combo.addItems(["KV Cache", "模型", "自定义"])
        self.table_type_combo.setToolTip("挂载成功后把本地目录加入「容器目录映射」表；"
                                         "KV Cache 映射到容器内相同路径")

        self.cmd_preview = QPlainTextEdit()
        self.cmd_preview.setReadOnly(True)
        self.cmd_preview.setFixedHeight(52)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.cmd_preview.setFont(font)

        self.mount_btn = QPushButton(ok_text)
        self.mount_btn.setProperty("accent", True)
        cancel_btn = QPushButton("取消")
        self.mount_btn.clicked.connect(self.mount_requested.emit)
        cancel_btn.clicked.connect(self.reject)

        form = QFormLayout()
        form.addRow("挂载方式", self.mode_combo)
        form.addRow(self.peer_label, self.peer_edit)
        form.addRow("远端 fs 名", self.fs_edit)
        form.addRow("本地挂载目录", self.dir_edit)
        form.addRow("加入映射表类型", self.table_type_combo)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("挂载命令（随输入实时生成，将在宿主机上执行）："))
        layout.addWidget(self.cmd_preview)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(cancel_btn)
        btns.addWidget(self.mount_btn)
        layout.addLayout(btns)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.fs_edit.textChanged.connect(self._on_fs_changed)
        self.dir_edit.textEdited.connect(self._on_dir_edited)
        for w in (self.peer_edit, self.fs_edit, self.dir_edit):
            w.textChanged.connect(self._update_preview)

        self._update_preview()

    # ---- 行为
    def _on_mode_changed(self, idx: int) -> None:
        if idx == 0:
            self.peer_label.setText("远端 IP")
            self.peer_edit.setPlaceholderText("NFS 服务器地址（如 192.168.1.10）")
        else:
            self.peer_label.setText("远端 CID")
            self.peer_edit.setPlaceholderText("DTFS 远端 CID（如 2200cc05777c3ef2）")
        self._update_preview()

    def _on_fs_changed(self, text: str) -> None:
        if not self._dir_dirty:
            fs = text.strip() or "fs1"
            self.dir_edit.setText(f"/mnt/{fs}")

    def _on_dir_edited(self, _text: str) -> None:
        self._dir_dirty = True

    def _update_preview(self, *_) -> None:
        self.cmd_preview.setPlainText(self.mount_command())

    # ---- 供调用方使用
    def mount_command(self) -> str:
        fs = self.fs_edit.text().strip() or "fs1"
        d = self.dir_edit.text().strip() or f"/mnt/{fs}"
        if self.mode_combo.currentIndex() == 0:
            peer = self.peer_edit.text().strip()
            return (f"mount -t nfs -o vers=3,nolock,proto=rdma,nconnect=8 "
                    f"{peer}:/{fs} {d}")
        peer = self.peer_edit.text().strip()
        return f"mount -t dtfs -o rw,eventual,cid={peer},dn=dns_local /{fs} {d}"

    def validate(self) -> Optional[str]:
        """返回错误提示文本；None 表示参数可用。"""
        if not self.peer_edit.text().strip():
            return f"请填写{self.peer_label.text()}"
        if not self.fs_edit.text().strip():
            return "请填写远端 fs 名"
        d = self.dir_edit.text().strip()
        if not d.startswith("/"):
            return "本地挂载目录必须是以 / 开头的绝对路径"
        return None

    def local_dir(self) -> str:
        return self.dir_edit.text().strip()


# ============================================================ 输入框拖动手柄
class _EditGrip(QWidget):
    """多行输入框右下角的拖动手柄：按住上下拖动调整输入框高度。

    浮在输入框内部右下角（不占布局空间），随输入框尺寸变化自动归位。
    自绘三条斜纹提示可拖拽；注意不能用 QSizeGrip——它会去缩放整个窗口。
    """

    MIN_H = 40
    MAX_H = 480

    def __init__(self, edit: QWidget, on_delta=None) -> None:
        super().__init__(edit)
        self._edit = edit
        self._on_delta = on_delta   # 高度变化回调（联动其他部件用）
        self._press_y = 0.0
        self._press_h = 0
        self.setFixedSize(12, 12)
        self.setCursor(Qt.SizeVerCursor)
        edit.installEventFilter(self)
        self._reposition()

    def eventFilter(self, obj, ev):  # noqa: N802 (Qt 命名)
        # PySide6 可能在 Python __init__ 完成前回调虚函数（属性未就绪）
        edit = getattr(self, "_edit", None)
        if edit is not None and obj is edit and ev.type() == QEvent.Resize:
            self._reposition()
        return False

    def showEvent(self, ev):  # noqa: N802 (Qt 命名)
        self._reposition()

    def _reposition(self) -> None:
        e = self._edit
        self.move(e.width() - self.width() - 1, e.height() - self.height() - 1)

    def mousePressEvent(self, ev) -> None:  # noqa: N802 (Qt 命名)
        if ev.button() == Qt.LeftButton:
            self._press_y = ev.globalPosition().y()
            self._press_h = self._edit.height()
            ev.accept()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802 (Qt 命名)
        if self._press_h:
            delta = ev.globalPosition().y() - self._press_y
            h = max(self.MIN_H, min(self._press_h + int(delta), self.MAX_H))
            old = self._edit.height()
            self._edit.setFixedHeight(h)
            if self._on_delta is not None and h != old:
                self._on_delta(h - old)
            ev.accept()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802 (Qt 命名)
        self._press_h = 0
        ev.accept()

    def paintEvent(self, ev) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setPen(self.palette().color(QPalette.Mid))
        w, h = self.width(), self.height()
        for i in range(1, 4):
            p.drawLine(w - i * 4, h - 1, w - 1, h - i * 4)


def attach_resize_grip(edit: QWidget, on_delta=None) -> _EditGrip:
    """给多行输入框右下角加拖动手柄，返回 grip 实例（随 edit 存活）。

    on_delta(delta)：输入框高度每变化 delta 时回调；可用于让页面上
    另一个可伸缩区域反向等量变化，保持页面总高稳定（布局不跳动）。
    """
    return _EditGrip(edit, on_delta)
