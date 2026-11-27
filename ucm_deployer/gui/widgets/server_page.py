# -*- coding: utf-8 -*-
"""步骤1：服务器管理 —— 登录信息保存/复用、单选登录、环境检测。"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.device_detector import (
    DeviceDetector,
    check_host_env,
    summarize_env_status,
)
from ...core.models import DeviceInfo, ServerInfo
from ..state import AppContext
from .common import ParallelTaskPanel


def _make_cell(text: str, server_id: str) -> QTableWidgetItem:
    """只读单元格：userData 携带 server_id（操作寻址与行序解耦）。"""
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
    item.setData(Qt.UserRole, server_id)
    return item


class ServerEditDialog(QDialog):
    """添加/编辑服务器登录信息。"""

    def __init__(self, parent=None, server: Optional[ServerInfo] = None):
        super().__init__(parent)
        self.setWindowTitle("编辑服务器" if server else "添加服务器")
        self.resize(460, 340)

        self.name_edit = QLineEdit(server.name if server else "")
        self.host_edit = QLineEdit(server.host if server else "")
        self.port_edit = QLineEdit(str(server.port if server else 22))
        self.user_edit = QLineEdit(server.username if server else "root")
        self.auth_combo = QComboBox()
        self.auth_combo.addItems(["密码", "私钥"])
        self.auth_combo.currentIndexChanged.connect(self._auth_changed)
        self.password_edit = QLineEdit(server.password if server else "")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.key_edit = QLineEdit(server.key_path if server else "")
        self.passphrase_edit = QLineEdit(server.passphrase if server else "")
        self.passphrase_edit.setEchoMode(QLineEdit.Password)
        self.remark_edit = QLineEdit(server.remark if server else "")

        self.key_browse_btn = QPushButton("浏览…")
        self.key_browse_btn.clicked.connect(self._browse_key)
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.key_edit)
        key_row.addWidget(self.key_browse_btn)
        key_holder = QWidget()
        key_holder.setLayout(key_row)

        form = QFormLayout()
        form.addRow("名称*", self.name_edit)
        form.addRow("主机 IP*", self.host_edit)
        form.addRow("SSH 端口", self.port_edit)
        form.addRow("用户名", self.user_edit)
        form.addRow("认证方式", self.auth_combo)
        form.addRow("密码", self.password_edit)
        form.addRow("私钥文件", key_holder)
        form.addRow("私钥口令", self.passphrase_edit)
        form.addRow("备注", self.remark_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        if server and server.auth_type == "key":
            self.auth_combo.setCurrentIndex(1)
        self._auth_changed()

    def _auth_changed(self) -> None:
        key_mode = self.auth_combo.currentIndex() == 1
        self.password_edit.setEnabled(not key_mode)
        self.key_edit.setEnabled(key_mode)
        self.passphrase_edit.setEnabled(key_mode)

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择私钥文件", "",
            "私钥文件 (*.pem *.key *.id_rsa *);;所有文件 (*)")
        if path:
            self.key_edit.setText(path)

    def _accept(self) -> None:
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "参数错误", "主机 IP 不能为空")
            return
        try:
            port = int(self.port_edit.text() or 22)
        except ValueError:
            QMessageBox.warning(self, "参数错误", "端口必须是数字")
            return
        if not 1 <= port <= 65535:
            QMessageBox.warning(self, "参数错误", "端口必须在 1–65535 之间")
            return
        self.accept()

    def build_server_info(self, base: Optional[ServerInfo] = None) -> ServerInfo:
        key_mode = self.auth_combo.currentIndex() == 1
        if base is None:
            info = ServerInfo.create(
                name=self.name_edit.text().strip() or self.host_edit.text().strip(),
                host=self.host_edit.text().strip(),
                port=int(self.port_edit.text() or 22),
                username=self.user_edit.text().strip() or "root",
                password="" if key_mode else self.password_edit.text(),
                auth_type="key" if key_mode else "password",
                key_path=self.key_edit.text().strip() if key_mode else "",
                passphrase=self.passphrase_edit.text() if key_mode else "",
                remark=self.remark_edit.text().strip(),
            )
        else:
            base.name = self.name_edit.text().strip() or base.host
            base.host = self.host_edit.text().strip()
            base.port = int(self.port_edit.text() or 22)
            base.username = self.user_edit.text().strip() or "root"
            base.auth_type = "key" if key_mode else "password"
            base.password = "" if key_mode else self.password_edit.text()
            base.key_path = self.key_edit.text().strip() if key_mode else ""
            base.passphrase = self.passphrase_edit.text() if key_mode else ""
            base.remark = self.remark_edit.text().strip()
            info = base
        return info


class ServerPage(QWidget):
    """步骤1 主页面（单服务器流程：一次只登录一台）。"""

    selection_changed = Signal()

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._detected: Dict[str, DeviceInfo] = {}
        self._rebuilding = False

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["名称", "IP", "端口", "用户", "设备", "卡数", "检测结果"])
        header = self.table.horizontalHeader()
        # 全部列平均分配宽度（用户偏好：先平均，需要时再手动拖动调整）
        for col in range(7):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        # 点选行 = 选定当前操作服务器（主从模式，取代复选框列）
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.cellDoubleClicked.connect(self._on_double_clicked)

        self.add_btn = QPushButton("➕ 添加服务器")
        self.edit_btn = QPushButton("✏ 编辑")
        self.del_btn = QPushButton("🗑 删除")
        self.test_btn = QPushButton("🔌 测试连接")
        self.detect_btn = QPushButton("🔍 环境检测")
        self.detect_btn.setToolTip(
            "环境检测（设备 + 宿主机）：探测加速设备（NPU/GPU）与 CPU 架构，\n"
            "并预检宿主机环境：驱动 / 内核 / SMMU / CANN 版本 / 内存 / Docker")
        self.batch_test_btn = QPushButton("🧪 批量测试连接")
        self.batch_test_btn.setToolTip("一次性测试列表中全部服务器的 SSH 连接")
        self.batch_detect_btn = QPushButton("🔍 批量环境检测")
        self.batch_detect_btn.setToolTip(
            "一次性对列表中全部服务器执行环境检测（设备 + 宿主机环境），\n"
            "并行执行、每台独立日志页签")
        self.add_btn.clicked.connect(self._add)
        self.edit_btn.clicked.connect(self._edit)
        self.del_btn.clicked.connect(self._delete)
        self.test_btn.clicked.connect(self._test_connections)
        self.detect_btn.clicked.connect(self._detect_devices)
        self.batch_test_btn.clicked.connect(lambda: self._test_connections(all_servers=True))
        self.batch_detect_btn.clicked.connect(lambda: self._detect_devices(all_servers=True))
        self.row_buttons = (self.edit_btn, self.del_btn,
                            self.test_btn, self.detect_btn)

        btns = QHBoxLayout()
        for b in (self.add_btn, *self.row_buttons,
                  self.batch_test_btn, self.batch_detect_btn):
            btns.addWidget(b)
        btns.addStretch(1)

        self.hint = QLabel(
            "点选一行 = 选定当前操作的服务器（一次一台）；双击行可编辑，右键有快捷操作。"
            "首次使用请在选定后点击「环境检测」，多台服务器可用批量按钮。")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#64748b;")
        self.hint.setToolTip(
            "登录信息保存在 ~/.ucm_deployer/servers.json，"
            "密码经 Fernet 加密（密钥同目录）；私钥路径仅保存在本地。")

        # 空状态：0 台服务器时的引导
        self.empty_hint = QLabel(
            "还没有服务器\n点击「添加服务器」录入第一台，"
            "随后环境检测即可进入步骤2")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setStyleSheet("color:#94a3b8; font-size:14px;")

        self.table_holder = QWidget()
        th = QVBoxLayout(self.table_holder)
        th.setContentsMargins(0, 0, 0, 0)
        th.addWidget(self.table)
        th.addWidget(self.empty_hint)

        self.panel = ParallelTaskPanel(split_layout=True)

        # 三段可拖分栏（约 4:3:3）：服务器列表 / 任务记录（检测结果）/
        # 日志页签——列表不再独占高度，检测结果与日志保持可用
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.table_holder)
        self.splitter.addWidget(self.panel)
        self.splitter.addWidget(self.panel.tabs)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setStretchFactor(2, 3)
        self.splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.addLayout(btns)
        layout.addWidget(self.hint)
        layout.addWidget(self.splitter, 1)
        self.refresh()      # 打开即显示历史保存的服务器

    def _make_env_cell(self, server_id: str) -> QTableWidgetItem:
        """「检测结果」列单元格：环境检测的总体结论
        （PASS 绿 / WARN 橙 / FAIL 红 / 未检测灰；悬浮显示明细与不通过项）。"""
        env = self.ctx.env_results.get(server_id) or []
        summary = summarize_env_status(env)
        item = QTableWidgetItem(summary or "未检测")
        item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        item.setData(Qt.UserRole, server_id)
        item.setTextAlignment(Qt.AlignCenter)
        if summary == "PASS":
            item.setForeground(Qt.darkGreen)
        elif summary == "WARN":
            item.setForeground(QColor("#d97706"))
        elif summary == "FAIL":
            item.setForeground(Qt.red)
        else:
            item.setForeground(Qt.gray)
        if env:
            lines = [f"[{it.status}] {it.name}  {it.summary}".rstrip()
                     for it in env]
            bad = [f"{it.name}：{it.summary}"
                   for it in env if it.status in ("WARN", "FAIL")]
            tip = "\n".join(lines)
            if bad:
                tip += "\n\n未通过项：\n" + "\n".join(bad)
            item.setToolTip(tip)
        return item

    def _flash_device_cell(self, server_id: str) -> None:
        """探测结果融入列表：该行设备单元格短暂绿色高亮后恢复默认。"""
        for row in range(self.table.rowCount()):
            head = self.table.item(row, 0)
            if head is not None and head.data(Qt.UserRole) == server_id:
                dev_item = self.table.item(row, 4)
                if dev_item is not None:
                    dev_item.setForeground(Qt.darkGreen)
                    QTimer.singleShot(
                        4000, lambda d=dev_item: d.setData(Qt.ForegroundRole, None))
                break

    def _update_action_states(self) -> None:
        has_selection = bool(self.ctx.selected)
        for b in self.row_buttons:
            b.setEnabled(has_selection)

    # ------------------------------------------------------------ 表格
    def refresh(self) -> None:
        self._rebuilding = True
        try:
            servers = self.ctx.registry.list_servers()
            self.table.setRowCount(0)
            selected_id = self.ctx.selected[0].id if self.ctx.selected else None
            for s in servers:
                row = self.table.rowCount()
                self.table.insertRow(row)
                name_text = s.name or "未命名"
                if not s.name or s.name == s.host:
                    name_text = f"{name_text}（未命名）"
                name_item = _make_cell(name_text, s.id)
                if not s.name or s.name == s.host:
                    name_item.setForeground(Qt.gray)
                self.table.setItem(row, 0, name_item)
                self.table.setItem(row, 1, _make_cell(s.host, s.id))
                self.table.setItem(row, 2, _make_cell(str(s.port), s.id))
                self.table.setItem(row, 3, _make_cell(s.username, s.id))
                dev = self.ctx.devices.get(s.id) or s.device
                dev = dev if (dev and dev.count) else None
                self.table.setItem(
                    row, 4, _make_cell(dev.display if dev else "未探测", s.id))
                self.table.setItem(
                    row, 5, _make_cell(str(dev.count) if dev else "—", s.id))
                self.table.setItem(row, 6, self._make_env_cell(s.id))
            self.empty_hint.setVisible(len(servers) == 0)
        finally:
            self._rebuilding = False
        # 恢复当前选中行（触发 _on_selection_changed 同步 ctx.selected）
        if selected_id is not None:
            for row in range(self.table.rowCount()):
                it = self.table.item(row, 0)
                if it is not None and it.data(Qt.UserRole) == selected_id:
                    self.table.setCurrentCell(row, 0)
                    break
        self._update_action_states()

    def _current_server(self) -> Optional[ServerInfo]:
        """当前操作对象：点选行的服务器（与所有操作按钮一致）。"""
        return self.ctx.selected[0] if self.ctx.selected else None

    def _on_selection_changed(self) -> None:
        if self._rebuilding:
            return
        self._collect_selection()
        self.selection_changed.emit()
        self._update_action_states()

    def _collect_selection(self) -> None:
        row = self.table.currentRow()
        selected: List[ServerInfo] = []
        if row >= 0:
            sid = self._row_server_id(row)
            if sid is not None:
                for s in self.ctx.registry.list_servers():
                    if s.id == sid:
                        selected.append(s)
                        break
        self.ctx.selected = selected
        self.ctx.reset_after_servers_changed()

    def _row_server_id(self, row: int) -> Optional[str]:
        if row < 0:
            return None
        it = self.table.item(row, 0)
        return it.data(Qt.UserRole) if it is not None else None

    def _on_double_clicked(self, row: int, _col: int) -> None:
        # 双击行：选定并打开编辑
        self.table.setCurrentCell(row, 0)
        if self._current_server() is not None:
            self._edit()

    def _show_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.setCurrentCell(row, 0)
        if self._current_server() is None:
            return
        menu = QMenu(self)
        act_test = menu.addAction("🔌 测试连接")
        act_detect = menu.addAction("🔍 环境检测")
        menu.addSeparator()
        act_edit = menu.addAction("✏ 编辑")
        act_del = menu.addAction("🗑 删除")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == act_test:
            self._test_connections()
        elif action == act_detect:
            self._detect_devices()
        elif action == act_edit:
            self._edit()
        elif action == act_del:
            self._delete()

    def ensure_selection(self) -> bool:
        self._collect_selection()
        if not self.ctx.selected:
            QMessageBox.warning(self, "未选择服务器", "请先点选一台服务器")
            return False
        return True

    # ------------------------------------------------------------ 操作
    def _add(self) -> None:
        dlg = ServerEditDialog(self)
        if dlg.exec() == QDialog.Accepted:
            info = dlg.build_server_info()
            existing = self.ctx.registry.list_servers()
            dup = next((s for s in existing
                        if s.host == info.host and s.port == info.port), None)
            if dup is not None:
                QMessageBox.warning(
                    self, "服务器已存在",
                    f"{info.host}:{info.port} 已在列表中（名称：{dup.name}）。")
                return
            self.ctx.registry.upsert(info)
            # 新增后自动选定（省去手动点选一步）
            self.ctx.selected = [info]
            self.ctx.devices.pop(info.id, None)
            self.ctx.reset_after_servers_changed()
            self.refresh()
            self.selection_changed.emit()
            self._flash_device_cell(info.id)

    def _edit(self) -> None:
        server = self._current_server()
        if server is None:
            QMessageBox.information(
                self, "提示", "请先点选要编辑的服务器行")
            return
        old_host, old_port = server.host, server.port
        dlg = ServerEditDialog(self, server)
        if dlg.exec() == QDialog.Accepted:
            info = dlg.build_server_info(server)
            if (info.host, info.port) != (old_host, old_port):
                dup = next((s for s in self.ctx.registry.list_servers()
                            if s.id != info.id and s.host == info.host
                            and s.port == info.port), None)
                if dup is not None:
                    QMessageBox.warning(
                        self, "服务器已存在",
                        f"{info.host}:{info.port} 已在列表中（名称：{dup.name}）。")
                    return
            self.ctx.registry.upsert(info)
            # 主机/端口可能已改变：旧设备信息必须失效，避免误导后续步骤
            # （表格设备列随即显示「未探测」，即重新探测的引导）
            self.ctx.devices.pop(info.id, None)
            self.ctx.env_results.pop(info.id, None)
            self.ctx.registry.touch(info.id, device=DeviceInfo())
            self.refresh()

    def _delete(self) -> None:
        server = self._current_server()
        if server is None:
            QMessageBox.information(
                self, "提示", "请先点选要删除的服务器行")
            return
        if QMessageBox.question(self, "确认删除",
                                f"删除服务器 {server.name} ({server.endpoint}) 的登录信息？"
                                ) == QMessageBox.Yes:
            self.ctx.registry.remove(server.id)
            self.ctx.selected = [s for s in self.ctx.selected if s.id != server.id]
            self.ctx.devices.pop(server.id, None)
            self.ctx.env_results.pop(server.id, None)
            self.ctx.reset_after_servers_changed()
            self.refresh()
            self.selection_changed.emit()

    def _test_connections(self, all_servers: bool = False) -> None:
        if all_servers:
            servers = self.ctx.registry.list_servers()
            if not servers:
                QMessageBox.information(self, "提示", "列表为空，请先添加服务器")
                return
        else:
            if not self.ensure_selection():
                return
            servers = self.ctx.selected

        def fn(ssh, tctx):
            tctx.progress(50, "SSH 握手成功")
            tctx.log(f"已连接 {ssh.server.endpoint} (用户 {ssh.server.username})")
            tctx.progress(100, "连接成功")

        title = ("批量测试连接（全部服务器）" if all_servers else "测试连接")
        self.panel.run_tasks([(s, fn) for s in servers], title)

    def _detect_devices(self, all_servers: bool = False) -> None:
        if all_servers:
            servers = self.ctx.registry.list_servers()
            if not servers:
                QMessageBox.information(self, "提示", "列表为空，请先添加服务器")
                return
        else:
            if not self.ensure_selection():
                return
            servers = self.ctx.selected
        self._detected = {}
        self._env_results: Dict[str, list] = {}

        def make_fn(server):
            def fn(ssh, tctx):
                tctx.progress(10, "环境检测...")
                dev = DeviceDetector(ssh).detect()
                tctx.log(f"探测结果: {dev.display}"
                         + (f" / {dev.arch}" if dev.arch else ""))
                if dev.device_type.value == "unknown" or dev.count <= 0:
                    raise RuntimeError("未探测到加速设备(NPU/GPU)")
                tctx.log("步骤2 构建镜像、步骤3 创建容器将按该设备类型自动适配")
                self._detected[server.id] = dev
                # 宿主机环境检测（驱动/内核/SMMU/CANN/内存/Docker）：提前
                # 环境复检部分检查，在准备镜像、创建容器之前先确认环境可用
                tctx.progress(55, "检测驱动/内核/SMMU/CANN/内存/Docker/共享内存/AIO ...")
                env = check_host_env(ssh, card_count=dev.count)
                self._env_results[server.id] = env
                for it in env:
                    tctx.log(f"[{it.status}] {it.name}  {it.summary}"
                             + (f"（{it.advice}）"
                                if it.advice and it.status in ("WARN", "FAIL")
                                else ""))
                fails = [it for it in env if it.status == "FAIL"]
                warns = [it for it in env if it.status == "WARN"]
                if fails:
                    tctx.log(
                        "[deployer] ⚠ 环境检测未通过"
                        f"（{len(fails)} 项 FAIL、{len(warns)} 项 WARN），"
                        "建议先修复后再准备镜像与创建容器")
                elif warns:
                    tctx.log(
                        "[deployer] 环境检测通过，"
                        f"但存在 {len(warns)} 项建议关注（见上）")
                else:
                    tctx.log("[deployer] 环境检测通过")
                tctx.progress(100, dev.display)
            return fn

        def on_finished(all_ok: bool) -> None:
            if not all_ok or not self._detected:
                QMessageBox.warning(self, "环境检测", "环境检测失败，详见日志")
                return
            self.ctx.devices.update(self._detected)
            # 环境检测结果写入共享状态：结果列与批量页目标表按此展示
            self.ctx.env_results.update(self._env_results)
            for sid, dev in self._detected.items():
                self.ctx.registry.touch(sid, device=dev)
            self.refresh()
            self.selection_changed.emit()
            # 反馈融入列表：检测到的行设备列短暂高亮
            for sid in self._detected:
                self._flash_device_cell(sid)
            # FAIL：明确提示（不强制拦截，详细建议在任务日志）
            for sid, env in self._env_results.items():
                fails = [it for it in env if it.status == "FAIL"]
                if not fails:
                    continue
                name = sid
                for s in self.ctx.registry.list_servers():
                    if s.id == sid:
                        name = s.name
                        break
                lines = "\n".join(
                    f"· {it.name}：{it.summary}\n  建议：{it.advice}"
                    for it in fails if it.advice)
                QMessageBox.warning(
                    self, "环境检测未通过",
                    f"服务器 {name} 存在不满足项：\n\n{lines}\n\n"
                    "建议先修复后再准备镜像与创建容器"
                    "（完整结果与建议见任务日志）。")

        title = ("批量环境检测（全部服务器）" if all_servers else "环境检测")
        self.panel.run_tasks([(s, make_fn(s)) for s in servers],
                             title, on_finished=on_finished)
