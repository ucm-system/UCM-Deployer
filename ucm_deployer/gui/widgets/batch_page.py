# -*- coding: utf-8 -*-
"""可选：批量部署 —— 在一台源服务器制作镜像后，向多台目标服务器
分发镜像、创建容器并执行环境预检。仅多台服务器场景需要本功能，
单台服务器走步骤5（服务部署）在目标容器内一键拉起推理服务。

- 镜像分发：镜像包中转（源服务器 docker save → 本机 → 目标 docker load）
  或镜像仓库（各目标直接 docker pull）；目标已有同名镜像可跳过；
- 目标服务器：从注册表勾选多台；登录信息/容器参数/预检参数可导出为
  数据模板（JSON）并在其他机器导入复用；
- 执行视图复用 ParallelTaskPanel：每台目标一行进度条 + 独立日志页签，
  tar 模式下源服务器作为首行先准备镜像包，其余目标等待就绪后并行分发。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.batch_deploy import (
    BatchContainerConfig,
    BatchDeployError,
    BatchPrecheckConfig,
    BatchStages,
    BatchTargetReport,
    DeployTemplate,
    TemplateError,
    create_container_from_template,
    export_image_tar,
    load_image_tar,
    pull_image_on_target,
    run_precheck_on_target,
    stage_progress_ranges,
    upsert_servers_from_template,
)
from ...core.docker_manager import DockerManager, validate_image_ref
from ...core.models import ServerInfo, VolumeMount
from ...core.shell import shq
from ..state import AppContext
from .common import ParallelTaskPanel, attach_resize_grip


class BatchPage(QWidget):
    """步骤5 主页面（多服务器批量部署）。"""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._target_ids: set = set()       # 勾选的目标服务器 id
        self._reports: Dict[str, BatchTargetReport] = {}
        self._tar_ready = threading.Event()
        self._tar_error = ""
        self._local_tar = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(10)              # 与步骤3/4 一致：模块间顺序感
        layout.addWidget(self._build_template_row())
        layout.addWidget(self._build_source_box())
        layout.addWidget(self._build_targets_box(), 1)
        layout.addWidget(self._build_config_box(), 1)
        layout.addWidget(self._build_run_box(), 1)

        self._update_target_count()

    # ------------------------------------------------------------ 界面构建
    def _build_template_row(self) -> QWidget:
        row = QWidget()
        import_btn = QPushButton("📥 导入部署模板")
        export_btn = QPushButton("📤 导出部署模板")
        import_btn.setToolTip(
            "从 JSON 数据模板导入：服务器登录信息（写入本机注册表）、\n"
            "镜像来源、容器创建参数与预检参数一次性填充")
        export_btn.setToolTip(
            "把当前勾选的目标服务器登录信息与镜像/容器/预检配置\n"
            "导出为 JSON 数据模板，可在其他机器导入复用")
        import_btn.clicked.connect(self._import_template)
        export_btn.clicked.connect(self._export_template)
        hint = QLabel("数据模板 = 登录信息 + 镜像来源 + 容器/预检配置；填一次，导出分享，他机导入即用")
        hint.setProperty("role", "hint")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(import_btn)
        lay.addWidget(export_btn)
        lay.addSpacing(12)
        lay.addWidget(hint, 1)
        return row

    def _build_source_box(self) -> QGroupBox:
        box = QGroupBox("1. 镜像来源（在一台服务器上已制作好的镜像）")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("镜像包中转：源服务器导出 → 本机 → 各目标导入")
        self.mode_combo.addItem("镜像仓库：各目标服务器直接 docker pull")
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(280)
        self.image_ref_edit = QLineEdit()
        self.image_ref_edit.setPlaceholderText(
            "镜像引用，如 ucm-vllm:v0.3.6（进入本页自动带入步骤2构建结果）")
        self.image_ref_edit.setMinimumWidth(300)
        self.skip_existing_check = QCheckBox("目标已存在同名镜像时跳过分发")
        self.skip_existing_check.setChecked(True)
        self.skip_existing_check.setToolTip(
            "勾选后逐台检查 docker image inspect：已存在则跳过上传/拉取，\n"
 "直接进入容器创建（适合增量补装或重复执行）")

        form = QFormLayout(box)
        form.addRow("分发方式", self.mode_combo)
        form.addRow("源服务器", self.source_combo)
        form.addRow("镜像", self.image_ref_edit)
        opts = QHBoxLayout()
        opts.addWidget(self.skip_existing_check)
        opts.addStretch(1)
        form.addRow("", opts)
        return box

    def _build_targets_box(self) -> QGroupBox:
        box = QGroupBox("2. 目标服务器（来自步骤1「服务器管理」，勾选多台）")
        select_all_btn = QPushButton("全选")
        clear_btn = QPushButton("清空")
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.setToolTip(
            "从步骤1「服务器管理」重新读取服务器列表（本页不单独维护服务器，\n"
            "增删改请回步骤1 操作后点此同步）")
        select_all_btn.clicked.connect(lambda: self._set_all_targets(True))
        clear_btn.clicked.connect(lambda: self._set_all_targets(False))
        refresh_btn.clicked.connect(self.refresh_servers)

        self.target_table = QTableWidget(0, 5)
        self.target_table.setHorizontalHeaderLabels(["部署", "名称", "地址", "用户", "设备"])
        header = self.target_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.target_table.verticalHeader().setVisible(False)
        self.target_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.target_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.target_table.setAlternatingRowColors(True)
        self.target_table.setMaximumHeight(126)
        self.target_table.itemChanged.connect(self._on_target_item_changed)

        self.target_count_label = QLabel("已选 0 台")
        self.target_count_label.setProperty("role", "hint")
        source_hint = QLabel(
            "服务器列表与步骤1「服务器管理」完全一致：在步骤1 新增/编辑后点「🔄 刷新」即可同步")
        source_hint.setProperty("role", "hint")

        btns = QHBoxLayout()
        for b in (select_all_btn, clear_btn, refresh_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        lay = QVBoxLayout(box)
        lay.setSpacing(4)
        lay.addLayout(btns)
        lay.addWidget(self.target_table, 1)
        count_row = QHBoxLayout()
        count_row.addWidget(self.target_count_label)
        count_row.addSpacing(12)
        count_row.addWidget(source_hint, 1)
        lay.addLayout(count_row)
        return box

    def _build_config_box(self) -> QGroupBox:
        box = QGroupBox("3. 部署配置模板（对全部目标生效，设备差异自动适配）")
        self.config_tabs = QTabWidget()
        self.config_tabs.addTab(self._build_container_tab(), "容器配置")
        self.config_tabs.addTab(self._build_precheck_tab(), "预检配置")
        lay = QVBoxLayout(box)
        lay.addWidget(self.config_tabs)
        return box

    def _build_container_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(4)

        # 基本参数行（标签改人话 + tooltip，与步骤3 一致）
        self.name_edit = QLineEdit("ucm-vllm")
        self.shm_edit = QLineEdit("512g")
        self.net_edit = QLineEdit("host")
        self.ports_edit = QLineEdit("8000:8000")
        self.shm_edit.setFixedWidth(80)
        self.net_edit.setFixedWidth(90)
        self.ports_edit.setFixedWidth(130)
        self.name_edit.setToolTip("docker 容器名（--name）；步骤4 按此名找到容器")
        self.shm_edit.setToolTip("容器共享内存（--shm-size）；vLLM 需要大共享内存，默认 512g")
        self.net_edit.setToolTip("host = 共享宿主机网络（Ascend 推荐）；NVIDIA 容器用端口映射")
        self.ports_edit.setToolTip("docker -p 端口映射（NVIDIA 容器使用，逗号分隔多个）")
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("容器名"))
        row1.addWidget(self.name_edit)
        row1.addWidget(QLabel("共享内存"))
        row1.addWidget(self.shm_edit)
        row1.addWidget(QLabel("网络"))
        row1.addWidget(self.net_edit)
        row1.addWidget(QLabel("端口映射"))
        row1.addWidget(self.ports_edit)
        row1.addStretch(1)
        lay.addLayout(row1)

        # 统一挂载表（与步骤3 一致）：KV Cache / 模型 / 自定义，一套按钮
        self.mount_type = QComboBox()
        self.mount_type.addItems(["KV Cache", "模型", "自定义"])
        self.mount_type.setToolTip(
            "KV Cache：挂载到容器内相同路径（多目录须位于同一文件系统）\n"
            "模型：模型文件目录（默认只读，表中最多一行）\n"
            "自定义：其他任意目录映射")
        self.mount_host_edit = QLineEdit()
        self.mount_host_edit.setPlaceholderText("宿主机绝对路径，如 /mnt/kvcache")
        self.mount_host_edit.textChanged.connect(self._sync_mount_container)
        self.mount_container_edit = QLineEdit()
        self.mount_container_edit.setPlaceholderText("容器内路径")
        mount_add_btn = QPushButton("➕ 添加")
        mount_add_btn.clicked.connect(self._mount_add)

        self.mount_table = QTableWidget(0, 4)
        self.mount_table.setHorizontalHeaderLabels(
            ["类型", "宿主机路径", "容器内路径", "只读"])
        self.mount_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.mount_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.mount_table.verticalHeader().setVisible(False)
        self.mount_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.mount_table.setMinimumHeight(150)
        self.mount_table.setToolTip(
            "所有挂载类型共用本表：上方输入后「➕ 添加」，选中行「🗑 移除」。\n"
            "KV Cache 行容器内路径自动与宿主机相同（锁定不可改）。\n"
            "「模型」行默认预置，可直接编辑；删除该行即不挂载模型。")
        self._add_mount_row("模型", "", "/models", True)   # 预置模型行（空路径=不挂载）
        mount_del_btn = QPushButton("🗑 移除选中")
        mount_del_btn.clicked.connect(self._mount_del)

        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("类型"))
        add_row.addWidget(self.mount_type)
        add_row.addWidget(self.mount_host_edit, 1)
        add_row.addWidget(QLabel("→ 容器内"))
        add_row.addWidget(self.mount_container_edit, 1)
        add_row.addWidget(mount_add_btn)
        add_row.addWidget(mount_del_btn)
        lay.addLayout(add_row)
        lay.addWidget(self.mount_table)

        # 附加参数
        extra_args_label = QLabel("附加 docker run 参数（每行一项，追加到命令末尾）")
        extra_args_label.setProperty("role", "hint")
        self.extra_args_edit = QPlainTextEdit()
        self.extra_args_edit.setFixedHeight(28)
        attach_resize_grip(self.extra_args_edit)   # 右下角拖动调整高度
        self.extra_args_edit.setPlaceholderText("--ulimit nofile=65535:65535")
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.extra_args_edit.setFont(font)
        lay.addWidget(extra_args_label)
        lay.addWidget(self.extra_args_edit)
        return page

    def _build_precheck_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(6)

        self.bandwidth_check = QCheckBox("执行存储带宽检查")
        self.bandwidth_check.setChecked(True)
        self.bandwidth_check.setToolTip(
            "开启：在挂载目录下创建临时子目录 ucm-precheck-* 执行读写负载，结束后自动清理；\n"
            "关闭：仅环境检查（驱动/内核/共享内存/AIO）")
        self.mount_edit = QLineEdit()
        self.mount_edit.setPlaceholderText("容器内挂载目录（应属于上方 KV Cache 目录之一）")
        self.mount_edit.setToolTip(
            "带宽测试目录（容器内路径）。须为容器创建参数中已映射进容器的目录，\n"
            "否则该服务器预检会报「挂载目录在容器内不存在」")
        row1 = QHBoxLayout()
        row1.addWidget(self.bandwidth_check)
        row1.addWidget(self.mount_edit, 1)
        lay.addLayout(row1)

        self.custom_cfg_check = QCheckBox("使用自定义预检配置（写入每个目标容器）")
        self.custom_cfg_check.setToolTip(
            "勾选后把下方 JSON 保存到每台目标容器内 /root/ucm-deploy/precheck.json，\n"
            "预检以 --config 引用（叠加于 ucm-toolkit 出厂默认之上，只需写要覆盖的键）。\n"
            "出厂默认可在步骤4「预检参数配置」中从容器读取参考。")
        self.custom_cfg_check.toggled.connect(self._validate_precheck_json)
        self.json_label = QLabel("")
        self.json_label.setProperty("role", "hint")
        row2 = QHBoxLayout()
        row2.addWidget(self.custom_cfg_check)
        row2.addStretch(1)
        row2.addWidget(self.json_label)
        lay.addLayout(row2)

        self.custom_cfg_edit = QPlainTextEdit()
        self.custom_cfg_edit.setPlaceholderText(
            '{\n  "bandwidth": {"workers": 8}\n}\n（JSON，只需写要覆盖的键）')
        self.custom_cfg_edit.setFont(QFont("Consolas"))
        self.custom_cfg_edit.setFixedHeight(84)
        attach_resize_grip(self.custom_cfg_edit)   # 右下角拖动调整高度
        self.custom_cfg_edit.textChanged.connect(self._validate_precheck_json)
        lay.addWidget(self.custom_cfg_edit)
        lay.addStretch(1)
        return page

    def _build_run_box(self) -> QGroupBox:
        box = QGroupBox("4. 批量执行（每台目标：分发 → 创建 → 预检，进度与日志按服务器分列）")
        self.stage_image_check = QCheckBox("镜像分发")
        self.stage_container_check = QCheckBox("容器创建")
        self.stage_precheck_check = QCheckBox("环境预检")
        for c in (self.stage_image_check, self.stage_container_check,
                  self.stage_precheck_check):
            c.setChecked(True)
        self.recreate_check = QCheckBox("同名容器先删除再创建")
        self.recreate_check.setToolTip(
            "重复执行批量部署时，目标服务器上已存在同名容器会导致 docker run 失败；\n"
            "勾选后在创建前先 docker rm -f 删除旧容器")

        self.run_btn = QPushButton("🚀 开始批量部署")
        self.run_btn.setProperty("accent", True)
        self.run_btn.clicked.connect(self._run)

        stages_row = QHBoxLayout()
        stages_row.addWidget(QLabel("执行阶段："))
        stages_row.addWidget(self.stage_image_check)
        stages_row.addWidget(self.stage_container_check)
        stages_row.addWidget(self.stage_precheck_check)
        stages_row.addWidget(self.recreate_check)
        stages_row.addStretch(1)
        stages_row.addWidget(self.run_btn)

        self.panel = ParallelTaskPanel()
        lay = QVBoxLayout(box)
        lay.setSpacing(6)
        lay.addLayout(stages_row)
        lay.addWidget(self.panel, 1)
        return box

    # ------------------------------------------------------------ 进入/刷新
    def on_enter(self) -> None:
        """进入本页：刷新服务器列表并带入步骤1/2 的单机流程结果。"""
        self.refresh_servers()
        server = self.ctx.server
        if server is not None:
            index = self._source_index_of(server.id)
            if index >= 0:
                self.source_combo.setCurrentIndex(index)
            if not self.image_ref_edit.text().strip():
                ref = self.ctx.images.get(server.id, "")
                if ref:
                    self.image_ref_edit.setText(ref)

    def refresh_servers(self) -> None:
        """重建目标服务器表（保留勾选状态）与源服务器下拉。"""
        servers = self.ctx.registry.list_servers()
        current_ids = {s.id for s in servers}
        self._target_ids &= current_ids
        # 保留源服务器当前选择
        current_source = str(self.source_combo.currentData(Qt.UserRole) or "")

        self.target_table.itemChanged.disconnect(self._on_target_item_changed)
        self.target_table.setRowCount(0)
        if not servers:
            # 空状态引导：与步骤1 的关系一目了然
            self.target_table.setRowCount(1)
            self.target_table.setSpan(0, 0, 1, 5)
            hint_item = QTableWidgetItem(
                "尚未添加服务器——请回步骤1「服务器管理」添加，然后点「🔄 刷新」同步")
            hint_item.setFlags(Qt.ItemIsEnabled)
            hint_item.setForeground(Qt.gray)
            self.target_table.setItem(0, 0, hint_item)
        for server in servers:
            row = self.target_table.rowCount()
            self.target_table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Checked if server.id in self._target_ids
                                else Qt.Unchecked)
            self.target_table.setItem(row, 0, check)
            self.target_table.setItem(row, 1, QTableWidgetItem(server.name))
            self.target_table.setItem(row, 2, QTableWidgetItem(server.endpoint))
            self.target_table.setItem(row, 3, QTableWidgetItem(server.username))
            dev_item = QTableWidgetItem(server.display_device)
            dev_item.setToolTip(server.remark)
            self.target_table.setItem(row, 4, dev_item)
        self.target_table.itemChanged.connect(self._on_target_item_changed)

        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        if not servers:
            self.source_combo.addItem("（注册表为空，请先在步骤1添加服务器）")
            self.source_combo.setItemData(0, "", Qt.UserRole)
        else:
            for server in servers:
                self.source_combo.addItem(f"{server.name} ({server.endpoint})")
                self.source_combo.setItemData(self.source_combo.count() - 1,
                                              server.id, Qt.UserRole)
            if current_source:
                index = self._source_index_of(current_source)
                if index >= 0:
                    self.source_combo.setCurrentIndex(index)
        self.source_combo.blockSignals(False)
        self._update_target_count()

    def _on_mode_changed(self) -> None:
        pull_mode = self.mode_combo.currentIndex() == 1
        self.source_combo.setEnabled(not pull_mode)
        if pull_mode:
            self.image_ref_edit.setPlaceholderText(
                "镜像仓库地址，如 quay.io/ascend/vllm-ascend:v0.30.0rc2")
        else:
            self.image_ref_edit.setPlaceholderText(
                "镜像引用，如 ucm-vllm:v0.3.6（进入本页自动带入步骤2构建结果）")

    def _on_target_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        row = item.row()
        servers = self.ctx.registry.list_servers()
        if row >= len(servers):
            return
        server_id = servers[row].id
        if item.checkState() == Qt.Checked:
            self._target_ids.add(server_id)
        else:
            self._target_ids.discard(server_id)
        self._update_target_count()

    def _set_all_targets(self, checked: bool) -> None:
        servers = self.ctx.registry.list_servers()
        if not servers:
            return
        if checked:
            self._target_ids = {s.id for s in servers}
        else:
            self._target_ids.clear()
        self.refresh_servers()

    def _update_target_count(self) -> None:
        self.target_count_label.setText(f"已选 {len(self._target_ids)} 台")

    def _selected_targets(self) -> List[ServerInfo]:
        selected = []
        for server in self.ctx.registry.list_servers():
            if server.id in self._target_ids:
                selected.append(server)
        return selected

    def _selected_source(self) -> Optional[ServerInfo]:
        server_id = str(self.source_combo.currentData(Qt.UserRole) or "")
        if not server_id:
            return None
        return self.ctx.registry.get(server_id)

    def _source_index_of(self, server_id: str) -> int:
        for i in range(self.source_combo.count()):
            if str(self.source_combo.itemData(i, Qt.UserRole) or "") == server_id:
                return i
        return -1

    # ------------------------------------------------------------ 挂载编辑（统一挂载表，与步骤3 一致）
    def _add_mount_row(self, mtype: str, host: str, cont: str,
                       readonly: bool = False) -> None:
        row = self.mount_table.rowCount()
        self.mount_table.insertRow(row)
        t_item = QTableWidgetItem(mtype)
        t_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.mount_table.setItem(row, 0, t_item)
        self.mount_table.setItem(row, 1, QTableWidgetItem(host))
        cont_item = QTableWidgetItem(cont)
        if mtype == "KV Cache":
            # KV Cache 行：容器内路径与宿主机相同，锁定不可编辑
            cont_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.mount_table.setItem(row, 2, cont_item)
        ro_item = QTableWidgetItem()
        ro_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        ro_item.setCheckState(Qt.Checked if readonly else Qt.Unchecked)
        self.mount_table.setItem(row, 3, ro_item)

    def _sync_mount_container(self, text: str) -> None:
        if self.mount_type.currentIndex() == 0:      # KV Cache：同路径
            self.mount_container_edit.setText(text.strip())

    def _mount_add(self) -> None:
        mtype = self.mount_type.currentText()
        host = self.mount_host_edit.text().strip()
        if not host.startswith("/"):
            QMessageBox.warning(self, "参数错误", "请输入以 / 开头的宿主机绝对路径")
            return
        if mtype == "模型":
            for row in range(self.mount_table.rowCount()):
                t_item = self.mount_table.item(row, 0)
                if (t_item is not None and t_item.text() == "模型"
                        and (self.mount_table.item(row, 1).text() or "").strip()):
                    QMessageBox.warning(self, "参数错误",
                                        "「模型」挂载最多一行（直接编辑预置行即可）")
                    return
        cont = self.mount_container_edit.text().strip()
        if mtype == "KV Cache":
            cont = host
        self._add_mount_row(mtype, host, cont, mtype != "KV Cache")
        self.mount_host_edit.clear()
        self.mount_container_edit.clear()
        self.mount_table.scrollToBottom()   # 新行立即可见

    def _mount_del(self) -> None:
        row = self.mount_table.currentRow()
        if row >= 0:
            self.mount_table.removeRow(row)

    # ------------------------------------------------------------ 配置收集
    def _build_container_template(self) -> BatchContainerConfig:
        ports = [p.strip() for p in re.split(r"[,，\s]+", self.ports_edit.text())
                 if p.strip()]
        kv_dirs: List[str] = []
        model_mount = None
        extra_mounts: List[VolumeMount] = []
        for row in range(self.mount_table.rowCount()):
            t_item = self.mount_table.item(row, 0)
            mtype = t_item.text() if t_item is not None else ""
            host = (self.mount_table.item(row, 1).text()
                    if self.mount_table.item(row, 1) else "").strip()
            cont = (self.mount_table.item(row, 2).text()
                    if self.mount_table.item(row, 2) else "").strip()
            ro_item = self.mount_table.item(row, 3)
            ro = ro_item is not None and ro_item.checkState() == Qt.Checked
            if not host:
                continue                     # 预置模型行空路径 = 不挂载模型
            if mtype == "KV Cache":
                kv_dirs.append(host)
            elif mtype == "模型":
                if model_mount is None:
                    model_mount = VolumeMount(host, cont or "/models", ro)
            elif cont:
                extra_mounts.append(VolumeMount(host, cont, ro))
        cfg = BatchContainerConfig(
            name=self.name_edit.text().strip() or "ucm-vllm",
            shm_size=self.shm_edit.text().strip() or "512g",
            network=self.net_edit.text().strip() or "host",
            ports=ports or ["8000:8000"],
            kv_cache_dirs=kv_dirs,
            model_mount=model_mount,
            extra_mounts=extra_mounts,
            extra_args=self.extra_args_edit.toPlainText())
        cfg.validate()
        return cfg

    def _build_precheck_template(self) -> BatchPrecheckConfig:
        cfg = BatchPrecheckConfig(
            bandwidth_check=self.bandwidth_check.isChecked(),
            mount_path=self.mount_edit.text().strip(),
            custom_config_enabled=self.custom_cfg_check.isChecked(),
            custom_config_json=self.custom_cfg_edit.toPlainText())
        cfg.validate()
        return cfg

    def _build_stages(self) -> BatchStages:
        return BatchStages(
            distribute_image=self.stage_image_check.isChecked(),
            create_container=self.stage_container_check.isChecked(),
            run_precheck=self.stage_precheck_check.isChecked())

    def _validate_precheck_json(self, *_) -> None:
        if not self.custom_cfg_check.isChecked():
            self.json_label.setText("")
            return
        text = self.custom_cfg_edit.toPlainText().strip()
        if not text:
            self.json_label.setText("配置为空（等效出厂默认）")
            return
        try:
            json.loads(text)
        except ValueError as exc:
            self.json_label.setText(f"✗ JSON 语法错误：{exc}")
        else:
            self.json_label.setText("✓ JSON 语法正确")

    # ------------------------------------------------------------ 模板导入/导出
    def _export_template(self) -> None:
        servers = self._selected_targets()
        include_secrets = False
        if any(s.password or s.passphrase for s in servers):
            include_secrets = QMessageBox.question(
                self, "导出登录密码",
                "所选服务器包含登录密码/私钥口令。\n"
                "是否在模板中包含（明文存储，导入后可直接登录）？\n\n"
                "包含：方便他机导入即用，但文件泄露即等于密码泄露；\n"
                "不包含：导入后需补填密码。") == QMessageBox.Yes
        try:
            tpl = DeployTemplate(
                image_mode="pull" if self.mode_combo.currentIndex() == 1 else "tar",
                image_ref=self.image_ref_edit.text().strip(),
                source_host=(self._selected_source().host
                             if self._selected_source() else ""),
                source_port=(self._selected_source().port
                             if self._selected_source() else 22),
                skip_existing=self.skip_existing_check.isChecked(),
                recreate_container=self.recreate_check.isChecked(),
                servers=servers,
                container=self._build_container_template(),
                precheck=self._build_precheck_template(),
                stages=self._build_stages())
        except Exception as exc:
            QMessageBox.warning(self, "配置无效", f"导出前请修正配置：\n{exc}")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "导出部署模板", "ucm-batch-template.json",
            "部署模板 (*.json);;所有文件 (*)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            tpl.save(path, include_secrets=include_secrets)
        except TemplateError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(
            self, "导出成功",
            f"部署模板已导出：\n{path}\n\n"
            f"包含 {len(servers)} 台服务器登录信息"
            + ("（含密码）" if include_secrets and servers else "")
            + "、镜像来源、容器与预检配置。")

    def _import_template(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入部署模板", "", "部署模板 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            tpl = DeployTemplate.load(path)
        except TemplateError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return

        # 先导入服务器（源服务器下拉需包含新导入条目），再回填其余配置
        imported, messages = upsert_servers_from_template(
            self.ctx.registry, tpl.servers)
        self._target_ids |= {s.id for s in imported}
        self.refresh_servers()
        self._apply_template(tpl)

        summary = "\n".join(messages) if messages else "（模板未包含服务器登录信息）"
        QMessageBox.information(
            self, "导入成功",
            f"部署模板已导入：\n{path}\n\n{summary}\n\n"
            "镜像/容器/预检配置已填充，目标服务器已自动勾选。")

    def _apply_template(self, tpl: DeployTemplate) -> None:
        """把模板内容回填到界面。"""
        self.mode_combo.setCurrentIndex(1 if tpl.image_mode == "pull" else 0)
        self.image_ref_edit.setText(tpl.image_ref)
        self.skip_existing_check.setChecked(tpl.skip_existing)
        self.recreate_check.setChecked(tpl.recreate_container)

        # 源服务器按 host:port 匹配
        if tpl.source_host:
            for i in range(self.source_combo.count()):
                sid = str(self.source_combo.itemData(i, Qt.UserRole) or "")
                server = self.ctx.registry.get(sid) if sid else None
                if (server is not None and server.host == tpl.source_host
                        and server.port == tpl.source_port):
                    self.source_combo.setCurrentIndex(i)
                    break

        cfg = tpl.container
        self.name_edit.setText(cfg.name)
        self.shm_edit.setText(cfg.shm_size)
        self.net_edit.setText(cfg.network)
        self.ports_edit.setText(" ".join(cfg.ports))
        # 统一挂载表回填：先预置空模型行，再按类型逐行填入
        self.mount_table.setRowCount(0)
        self._add_mount_row("模型", "", "/models", True)
        for d in cfg.kv_cache_dirs:
            self._add_mount_row("KV Cache", d, d, False)
        if cfg.model_mount is not None:
            self.mount_table.item(0, 1).setText(cfg.model_mount.host_path)
            self.mount_table.item(0, 2).setText(cfg.model_mount.container_path)
            self.mount_table.item(0, 3).setCheckState(
                Qt.Checked if cfg.model_mount.readonly else Qt.Unchecked)
        for m in cfg.extra_mounts:
            self._add_mount_row("自定义", m.host_path, m.container_path, m.readonly)
        self.extra_args_edit.setPlainText(cfg.extra_args)

        pre = tpl.precheck
        self.bandwidth_check.setChecked(pre.bandwidth_check)
        self.mount_edit.setText(pre.mount_path)
        self.custom_cfg_check.setChecked(pre.custom_config_enabled)
        self.custom_cfg_edit.setPlainText(pre.custom_config_json)
        self._validate_precheck_json()

        self.stage_image_check.setChecked(tpl.stages.distribute_image)
        self.stage_container_check.setChecked(tpl.stages.create_container)
        self.stage_precheck_check.setChecked(tpl.stages.run_precheck)

    # ------------------------------------------------------------ 批量执行
    def _run(self) -> None:
        if self.panel.is_running():
            QMessageBox.warning(self, "忙碌", "批量任务正在执行，请等待完成或取消")
            return
        servers = self._selected_targets()
        if not servers:
            QMessageBox.warning(self, "未选择目标服务器",
                                "请在「2. 目标服务器」勾选至少一台目标服务器")
            return
        image_ref = self.image_ref_edit.text().strip()
        try:
            validate_image_ref(image_ref)
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return

        mode = "pull" if self.mode_combo.currentIndex() == 1 else "tar"
        source = None
        if mode == "tar":
            source = self._selected_source()
            if source is None:
                QMessageBox.warning(self, "参数错误", "镜像包中转方式需要选择源服务器")
                return

        stages = self._build_stages()
        if not (stages.distribute_image or stages.create_container
                or stages.run_precheck):
            QMessageBox.warning(self, "参数错误", "请至少勾选一个执行阶段")
            return
        try:
            container_tpl = self._build_container_template()
            precheck_tpl = self._build_precheck_template()
        except BatchDeployError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return

        # 重置共享状态
        self._reports = {}
        self._cleanup_local_tar()
        self._tar_ready = threading.Event()
        self._tar_error = ""

        tasks: List[Tuple[ServerInfo, object]] = []
        if mode == "tar":
            tasks.append((source, self._make_prepare_task(source, image_ref)))
        for server in servers:
            tasks.append((server, self._make_deploy_task(
                server, image_ref, mode, stages, container_tpl, precheck_tpl)))

        self.run_btn.setEnabled(False)
        title = (f"批量部署（{len(servers)} 台目标"
                 + ("，源服务器先准备镜像包" if mode == "tar" else "")
                 + "）")
        if not self.panel.run_tasks(tasks, title, on_finished=self._on_finished):
            self.run_btn.setEnabled(True)
            self._cleanup_local_tar()

    def _make_prepare_task(self, source: ServerInfo, image_ref: str):
        """源服务器任务：docker save 导出并下载镜像包到本机。"""
        uid = uuid.uuid4().hex[:8]
        local = os.path.join(tempfile.gettempdir(), f"ucm-batch-{uid}.tar.gz")
        remote = f"/tmp/ucm-batch-{uid}.tar.gz"
        self._local_tar = local

        def fn(ssh, tctx):
            try:
                tctx.progress(5, "docker save 导出镜像（可能需要数分钟）...")
                export_image_tar(ssh, DockerManager(ssh), image_ref, remote,
                                 log=tctx.log, cancelled=tctx.cancelled)
                tctx.progress(50, "下载镜像包到本机...")
                size = ssh.file_size(remote)
                tctx.log(f"镜像包大小: {size / 1e6:.1f} MB")

                def cb(sent: int, total: int) -> None:
                    tctx.progress(
                        int(50 + 45 * sent / max(total, 1)),
                        f"下载镜像包 {sent / 1e6:.0f}/{total / 1e6:.0f} MB")

                try:
                    ssh.download_file(remote, local, progress_cb=cb)
                finally:
                    try:
                        ssh.exec(f"rm -f {shq(remote)}", timeout=60)
                    except Exception:
                        pass
                tctx.progress(100, "镜像包准备完成（等待目标分发）")
            except Exception as exc:
                self._tar_error = str(exc)
                self._tar_ready.set()
                raise
            else:
                self._tar_ready.set()

        return fn

    def _make_deploy_task(self, server: ServerInfo, image_ref: str, mode: str,
                          stages: BatchStages, container_tpl: BatchContainerConfig,
                          precheck_tpl: BatchPrecheckConfig):
        """一台目标服务器的流水线任务：分发 -> 创建容器 -> 预检。"""
        ranges = stage_progress_ranges(stages)
        skip_existing = self.skip_existing_check.isChecked()
        recreate = self.recreate_check.isChecked()
        report = BatchTargetReport(server_id=server.id,
                                   server_name=server.name, image=image_ref)

        def fn(ssh, tctx):
            def stage_progress(stage: str, pct: int, msg: str) -> None:
                lo, hi = ranges.get(stage, (0, 100))
                pct = max(0, min(100, int(pct)))
                tctx.progress(lo + int((hi - lo) * pct / 100), msg)

            docker = DockerManager(ssh)

            # ---- 阶段1：镜像分发
            if stages.distribute_image:
                stage_progress("image", 2, "准备镜像...")
                if mode == "pull":
                    pull_image_on_target(
                        docker, image_ref,
                        progress=lambda p, m: stage_progress("image", p, m),
                        log=tctx.log)
                else:
                    if not self._tar_ready.is_set():
                        tctx.log("等待源服务器镜像包准备完成...")
                    while not self._tar_ready.wait(timeout=0.2):
                        if tctx.cancelled():
                            raise BatchDeployError("已取消（等待镜像包）")
                    if self._tar_error:
                        raise BatchDeployError(
                            f"源服务器镜像包准备失败: {self._tar_error}")
                    if skip_existing and docker.image_exists(image_ref):
                        report.image_skipped = True
                        tctx.log(f"镜像 {image_ref} 已存在，跳过分发")
                        stage_progress("image", 100, "镜像已存在，跳过分发")
                    else:
                        load_image_tar(
                            ssh, docker, self._local_tar, image_ref,
                            progress=lambda p, m: stage_progress("image", p, m),
                            log=tctx.log, cancelled=tctx.cancelled)

            # ---- 阶段2：容器创建
            if stages.create_container:
                name, _cmd = create_container_from_template(
                    ssh, docker, container_tpl, image_ref,
                    progress=lambda p, m: stage_progress("container", p, m),
                    log=tctx.log, recreate=recreate)
                report.container = name

            # ---- 阶段3：环境预检
            if stages.run_precheck:
                container = report.container or container_tpl.name
                tctx.log(f"预检目标容器: {container}")
                result = run_precheck_on_target(
                    ssh, precheck_tpl, container,
                    progress=lambda p, m: stage_progress("precheck", p, m),
                    log=tctx.log)
                report.precheck_summary = result.summary
                report.precheck_verdict = result.verdict

            self._reports[server.id] = report
            tctx.progress(100, report.describe())

        return fn

    def _on_finished(self, all_ok: bool) -> None:
        self.run_btn.setEnabled(True)
        self._cleanup_local_tar()
        if not self._reports:
            QMessageBox.warning(
                self, "批量部署未完成",
                "没有任何目标服务器完成，请展开各服务器日志页签查看失败原因。")
            return

        lines = []
        failed_reports = [r for r in self._reports.values()
                          if r.precheck_verdict == "failed"]
        for server in self._selected_targets():
            rep = self._reports.get(server.id)
            if rep is None:
                continue
            icon = ("✗" if rep.precheck_verdict == "failed" else "✓")
            lines.append(f"{icon} {rep.server_name}: {rep.describe()}"
                         + (f"\n    {rep.precheck_summary}" if rep.precheck_summary
                            and rep.precheck_verdict == "failed" else ""))
        message = (f"完成 {len(self._reports)} 台"
                   + (f"，其中 {len(failed_reports)} 台预检未通过" if failed_reports else "")
                   + "：\n\n" + "\n".join(lines))
        if failed_reports or not all_ok:
            QMessageBox.warning(self, "批量部署完成（有失败）", message)
        else:
            QMessageBox.information(self, "批量部署完成", message)

    def _cleanup_local_tar(self) -> None:
        if self._local_tar and os.path.isfile(self._local_tar):
            try:
                os.remove(self._local_tar)
            except OSError:
                pass
        self._local_tar = ""
