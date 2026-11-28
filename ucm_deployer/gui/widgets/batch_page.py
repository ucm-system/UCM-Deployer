# -*- coding: utf-8 -*-
"""可选：批量部署 —— 在一台源服务器制作镜像后，向多台目标服务器
分发镜像、创建容器并执行环境预检。仅多台服务器场景需要本功能，
单台服务器走步骤5（推理服务拉起）在目标容器内一键拉起推理服务。

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
import time
import uuid
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
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
    BatchUcmConfigConfig,
    DeployTemplate,
    TemplateError,
    create_container_from_template,
    export_image_tar,
    load_image_tar,
    load_remote_tar,
    pull_image_on_target,
    run_precheck_on_target,
    scp_image_between,
    stage_progress_ranges,
    upsert_servers_from_template,
    write_ucm_config_on_target,
)
from ...core.container_manager import (UCM_CONFIG_DEFAULT_PATH,
                                       UCM_CONFIG_TEMPLATE)
from ...core.docker_manager import DockerManager, validate_image_ref
from ...core.device_detector import summarize_env_status
from ...core.models import ServerInfo, VolumeMount
from ...core.shell import shq
from ..state import AppContext
from .common import ParallelTaskPanel, RemoteMountDialog, attach_resize_grip

# 自定义预检配置默认模板（叠加于 ucm-toolkit 出厂默认之上，只需写要覆盖的键；
# 常用键参考 ucm-toolkit 出厂默认 precheck.defaults.json / 步骤4「预检参数配置」）
DEFAULT_PRECHECK_TEMPLATE = """{
  "kernel_min": "5.10",
  "bandwidth": {
    "workers": 8,
    "threshold_gb": 8.0
  }
}"""

# 「执行存储带宽检查」提示：可用态 / 无 KV Cache 挂载时的禁用态
BANDWIDTH_TIP = (
    "开启：在挂载目录下创建临时子目录 ucm-precheck-* 执行读写负载，结束后自动清理；\n"
    "关闭：仅环境检查（驱动/内核/共享内存/AIO）")
BANDWIDTH_TIP_NO_KV = (
    "带宽检查在 KV Cache 挂载目录下执行读写测试。\n"
    "请先在「容器配置」页签的挂载表中添加 KV Cache 目录")


class BatchPage(QWidget):
    """（可选）批量部署主页面（位于步骤4 与步骤5 之间）。"""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._target_ids: set = set()       # 勾选的目标服务器 id
        self._reports: Dict[str, BatchTargetReport] = {}
        self._tar_ready = threading.Event()
        self._tar_error = ""
        self._local_tar = ""
        self._src_tar = ""          # 直传模式：源服务器上的镜像包路径

        layout = QVBoxLayout(self)
        layout.setSpacing(10)              # 与步骤3/4 一致：模块间顺序感
        layout.addWidget(self._build_template_row())
        layout.addWidget(self._build_targets_box())

        # 上半（部署配置）与下半（批量执行 + 任务面板）可拖分栏：任务启动后
        # 进度表 + 各服务器日志需要更多高度，固定 stretch 会把配置模板压扁
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_config_box())
        splitter.addWidget(self._build_run_box())
        splitter.setStretchFactor(0, 6)
        splitter.setStretchFactor(1, 4)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        self._update_target_count()

    # ------------------------------------------------------------ 界面构建
    def _build_template_row(self) -> QWidget:
        row = QWidget()
        import_btn = QPushButton("📥 导入部署模板")
        export_btn = QPushButton("📤 导出部署模板")
        import_btn.setToolTip(
            "从 JSON 部署模板导入整套配置：服务器登录信息（自动写入服务器管理\n"
            "并勾选为目标）+ 镜像与分发 + 容器/预检配置 + 执行阶段开关")
        export_btn.setToolTip(
            "把整套批量部署配置导出为 JSON 模板（密码默认不包含），\n"
            "供其他电脑导入复用")
        import_btn.clicked.connect(self._import_template)
        export_btn.clicked.connect(self._export_template)
        hint = QLabel("整套配置（服务器 + 分发 + 容器/预检）导出 JSON，他机导入即用")
        hint.setProperty("role", "hint")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(import_btn)
        lay.addWidget(export_btn)
        lay.addSpacing(12)
        lay.addWidget(hint, 1)
        return row

    def _build_source_controls(self) -> None:
        """「镜像与分发」页签的控件（在 _build_config_box 中先建后入页签）。

        分发前自动检查目标已有同名镜像：已存在则跳过上传/导入并在日志说明
        （分发目标就是让镜像就位，已存在即视为成功，不再提供开关）。"""
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("镜像包中转：源服务器导出 → 本机 → 各目标导入")
        self.mode_combo.addItem("镜像仓库：各目标服务器直接 docker pull")
        self.mode_combo.addItem("服务器直传：源服务器导出 → 服务器间 scp → 各目标导入")
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(280)
        self.image_ref_edit = QLineEdit()
        self.image_ref_edit.setPlaceholderText(
            "镜像引用，如 ucm-vllm:v0.3.6（进入本页自动带入步骤2构建结果）")
        self.image_ref_edit.setMinimumWidth(300)
        self.image_ref_edit.setToolTip(
            "创建容器与预检都使用此镜像。\n"
            "未勾选「分发镜像」阶段时，此镜像须已存在于各目标服务器。")
        self.tar_remote_edit = QLineEdit("/tmp")
        self.tar_remote_edit.setToolTip(
            "镜像包上传到各目标服务器后的保存目录（绝对路径）。\n"
            "docker load 校验完成后会自动删除该临时包；\n"
            "大镜像包建议指定大容量目录（如 /data），避免 /tmp 空间不足。")

    def _build_source_tab(self) -> QWidget:
        """「镜像与分发」页签：镜像从哪来 + 分发方式 + 目标保存路径。

        分发相关控件集中在 _distribute_widget（取消「分发镜像」阶段时
        整体禁用）；镜像名始终可编辑——创建容器/预检都要用它。"""
        page = QWidget()
        form = QFormLayout(page)
        self._source_hint = QLabel("")
        self._source_hint.setProperty("role", "hint")
        self._source_hint.setWordWrap(True)
        form.addRow(self._source_hint)
        self._distribute_widget = QWidget()
        dform = QFormLayout(self._distribute_widget)
        dform.setContentsMargins(0, 0, 0, 0)
        dform.addRow("分发方式", self.mode_combo)
        dform.addRow("源服务器", self.source_combo)
        dform.addRow("目标保存路径", self.tar_remote_edit)
        form.addRow(self._distribute_widget)
        form.addRow("镜像", self.image_ref_edit)
        return page

    def _build_targets_box(self) -> QGroupBox:
        box = QGroupBox("1. 目标服务器（勾选多台，来自步骤1「服务器管理」）")
        # 勾选交互（不再放全选/清空/刷新按钮，减少控件堆砌）：
        # - 点击行任意位置切换该台的部署勾选（勾选行整行高亮，足够显眼）；
        # - 点「部署」列表头 = 全选 / 清空（列头带 ☑/◐/☐ 三态标记）；
        # - 表格空白处右键 = 全选 / 清空 / 刷新列表
        self.target_table = QTableWidget(0, 6)
        self.target_table.setHorizontalHeaderLabels(
            ["部署", "名称", "地址", "用户", "设备", "环境"])
        header = self.target_table.horizontalHeader()
        # 「部署」勾选列窄列，其余列平均分配宽度（用户偏好：先平均，
        # 需要时再手动拖动调整）
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for col in (1, 2, 3, 4, 5):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        header.setSectionsClickable(True)
        self.target_table.horizontalHeaderItem(0).setToolTip(
            "点击列头：全选 / 清空全部目标")
        self.target_table.horizontalHeaderItem(5).setToolTip(
            "步骤1「环境检测」时宿主机环境检测的汇总结果：\n"
            "PASS=全部通过（可放心用于批量部署）；WARN=有建议关注项；\n"
            "FAIL=有不满足项（建议先修复）；未检测=尚未执行步骤1「环境检测」")
        self.target_table.verticalHeader().setVisible(False)
        self.target_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.target_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.target_table.setAlternatingRowColors(True)
        self.target_table.setToolTip(
            "点击行任意位置即可勾选/取消该台目标；\n"
            "点「部署」列表头全选或清空；右键菜单可刷新列表")
        # 至少可见 3 行目标服务器（旧版 max=126 且被任务面板挤压后只剩 1 行）
        self.target_table.setMinimumHeight(114)
        self.target_table.itemChanged.connect(self._on_target_item_changed)
        # 点击行任意位置切换勾选（第 0 列 checkbox 自带切换，避免重复翻转）
        self.target_table.cellClicked.connect(self._on_target_row_clicked)
        header.sectionClicked.connect(self._on_target_header_clicked)
        # 右键菜单：全选 / 清空 / 刷新
        self.target_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.target_table.customContextMenuRequested.connect(
            self._show_target_menu)

        self.target_count_label = QLabel("已选 0 台")
        self.target_count_label.setProperty("role", "hint")
        source_hint = QLabel(
            "列表与步骤1「服务器管理」完全一致：步骤1 增删改后右键「刷新列表」同步")
        source_hint.setProperty("role", "hint")

        lay = QVBoxLayout(box)
        lay.setSpacing(4)
        lay.addWidget(self.target_table, 1)
        count_row = QHBoxLayout()
        count_row.addWidget(self.target_count_label)
        count_row.addSpacing(12)
        count_row.addWidget(source_hint, 1)
        lay.addLayout(count_row)
        return box

    def _build_config_box(self) -> QGroupBox:
        box = QGroupBox("2. 部署配置（对全部目标生效，设备差异自动适配）")
        # 三个执行阶段开关集中放页签栏右上角（一组可选步骤，风格统一）：
        # 分发镜像 / 创建容器 / 环境预检——取消勾选则跳过对应阶段，
        # 勾选变化实时刷新模块3 的执行说明
        self.stage_image_check = QCheckBox("分发镜像")
        self.stage_image_check.setToolTip(
            "勾选 = 批量执行时包含「镜像分发」阶段；\n"
            "取消勾选则跳过分发（镜像须已存在于目标服务器），仅创建容器/预检")
        self.stage_container_check = QCheckBox("创建容器并配置UCM文件")
        self.stage_container_check.setToolTip(
            "勾选 = 批量执行时为每台目标创建容器（「容器配置」页签）并在创建\n"
            "完成后写入 UCM 配置文件（「UCM文件配置」页签，容器名即「容器配置」\n"
            "页签中的容器名，全目标统一）；写入失败会导致该台目标部署失败。\n"
            "取消勾选则跳过容器创建，且「容器配置」「UCM文件配置」两页签均不可选中")
        self.stage_precheck_check = QCheckBox("环境预检")
        self.stage_precheck_check.setToolTip(
            "勾选 = 批量执行时包含「环境预检」阶段")
        for c in (self.stage_image_check, self.stage_container_check,
                  self.stage_precheck_check):
            c.setChecked(True)

        # 镜像来源控件先建（模式切换联动依赖），再作为首个页签加入
        self._build_source_controls()

        self.config_tabs = QTabWidget()
        self.config_tabs.addTab(self._build_source_tab(), "镜像与分发")
        self.config_tabs.addTab(self._build_container_tab(), "容器配置")
        self.config_tabs.addTab(self._build_ucm_cfg_tab(), "UCM文件配置")
        self.config_tabs.addTab(self._build_precheck_tab(), "预检配置")
        corner = QWidget()
        corner_lay = QHBoxLayout(corner)
        corner_lay.setContentsMargins(0, 0, 8, 0)
        corner_lay.setSpacing(12)
        corner_lay.addWidget(self.stage_image_check)
        corner_lay.addWidget(self.stage_container_check)
        corner_lay.addWidget(self.stage_precheck_check)
        self.config_tabs.setCornerWidget(corner, Qt.TopRightCorner)
        # 取消勾选 -> 对应页签置灰（不可编辑），配置仍保留
        # 「镜像与分发」页签特殊：页签保持可编辑（镜像名始终要填——
        # 创建容器/预检都使用它），只禁用其中的分发参数区
        # 「容器配置」与「UCM文件配置」两页签随右上角「创建容器并配置
        # UCM 文件」开关联动（取消时均不可选中）
        self.stage_image_check.toggled.connect(self._on_image_stage_toggled)
        self.stage_container_check.toggled.connect(
            self._update_config_tab_states)
        self.stage_precheck_check.toggled.connect(
            lambda on: self.config_tabs.setTabEnabled(3, on))
        # 勾选变化 -> 模块3 执行说明实时刷新
        self.stage_image_check.toggled.connect(self._update_exec_summary)
        self.stage_container_check.toggled.connect(self._update_exec_summary)
        self.stage_precheck_check.toggled.connect(self._update_exec_summary)
        # 初始页签状态同步（「创建容器并配置UCM文件」默认勾选）
        self._update_config_tab_states()

        lay = QVBoxLayout(box)
        lay.addWidget(self.config_tabs)
        return box

    def _update_config_tab_states(self) -> None:
        """右上角「创建容器并配置UCM文件」联动容器配置/UCM文件配置两页签。

        - 未勾选：两页签均不可选中（点击无效，配置保留，页内控件不禁用）；
        - 勾选：批量执行时创建容器（「容器配置」页签）并在每台容器创建
          完成后写入 UCM 配置（「UCM文件配置」页签）。"""
        on = self.stage_container_check.isChecked()
        bar = self.config_tabs.tabBar()
        bar.setTabEnabled(1, on)
        bar.setTabEnabled(2, on)

    def _on_image_stage_toggled(self, on: bool) -> None:
        """取消「分发镜像」时只禁用分发参数区；镜像名保持可编辑。"""
        self._distribute_widget.setEnabled(on)
        self._source_hint.setText(
            "" if on else
            "未勾选「分发镜像」：镜像须已存在于各目标服务器，"
            "创建容器与预检将直接使用下方镜像名")

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
        self.name_edit.setToolTip("docker 容器名（--name）；步骤4 复检按此名找到容器")
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
        # 同名容器先删除再创建（容器创建行为，随「容器配置」同处）
        self.recreate_check = QCheckBox("同名容器先删除再创建")
        self.recreate_check.setToolTip(
            "重复执行批量部署时，目标服务器上已存在同名容器会导致 docker run 失败；\n"
            "勾选后在创建前先 docker rm -f 删除旧容器")
        row1.addWidget(self.recreate_check)
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
        remote_mount_btn = QPushButton("🌐 批量新增远端挂载")
        remote_mount_btn.setToolTip(
            "在全部勾选的目标服务器宿主机上挂载远端文件系统"
            "（NFS over RDMA / DTFS），\n挂载成功后自动把本地目录加入上方映射表")
        remote_mount_btn.clicked.connect(self._remote_mount)

        self.mount_table = QTableWidget(0, 4)
        self.mount_table.setHorizontalHeaderLabels(
            ["类型", "宿主机路径", "容器内路径", "只读"])
        self.mount_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.mount_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.mount_table.verticalHeader().setVisible(False)
        self.mount_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.mount_table.setMinimumHeight(150)
        self.mount_table.setToolTip(
            "所有挂载类型共用本表：上方输入后「➕ 添加」；\n"
            "**右键点击行**可「移除选中」「批量校验」。\n"
            "KV Cache 行容器内路径自动与宿主机相同（锁定不可改）。\n"
            "默认表为空（即不挂载任何目录）；需要挂载模型时选择类型后添加。")
        # 挂载表变化联动「执行存储带宽检查」可用性（需存在 KV Cache 目录）
        mount_model = self.mount_table.model()
        mount_model.rowsInserted.connect(self._update_bandwidth_state)
        mount_model.rowsRemoved.connect(self._update_bandwidth_state)
        self.mount_table.itemChanged.connect(self._update_bandwidth_state)
        self.mount_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.mount_table.customContextMenuRequested.connect(self._mount_menu)

        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("类型"))
        add_row.addWidget(self.mount_type)
        add_row.addWidget(self.mount_host_edit, 1)
        add_row.addWidget(QLabel("→ 容器内"))
        add_row.addWidget(self.mount_container_edit, 1)
        add_row.addWidget(mount_add_btn)
        add_row.addWidget(remote_mount_btn)
        lay.addLayout(add_row)
        lay.addWidget(self.mount_table)

        # 附加参数（与「自定义 docker run 命令」互斥：勾选自定义后置灰）
        self.extra_args_label = QLabel("附加 docker run 参数（每行一项，追加到命令末尾）")
        self.extra_args_label.setProperty("role", "hint")
        self.extra_args_edit = QPlainTextEdit()
        self.extra_args_edit.setFixedHeight(28)
        attach_resize_grip(self.extra_args_edit)   # 右下角拖动调整高度
        self.extra_args_edit.setPlaceholderText("--ulimit nofile=65535:65535")
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.extra_args_edit.setFont(font)
        lay.addWidget(self.extra_args_label)
        lay.addWidget(self.extra_args_edit)

        # ---- 自定义 docker run 命令（可选；所有目标使用同一条）
        self.custom_cmd_check = QCheckBox(
            "自定义 docker run 命令（所有目标使用同一条，跳过逐台设备适配）")
        self.custom_cmd_check.setToolTip(
            "勾选后批量部署直接执行下方命令，不再按每台服务器现场探测结果"
            "生成命令。\n适合设备完全一致的同构集群；异构集群请保持不勾选。\n"
            "命令中可写 {image} 占位符，执行时自动替换为所选镜像；"
            "容器名请与上方「容器名」保持一致（用于同名容器检测与状态确认）。")
        self.custom_cmd_edit = QPlainTextEdit()
        self.custom_cmd_edit.setEnabled(False)
        self.custom_cmd_edit.setFixedHeight(84)
        attach_resize_grip(self.custom_cmd_edit)
        self.custom_cmd_edit.setFont(font)
        self.custom_cmd_edit.setPlaceholderText(
            "docker run -itd --name ucm-vllm --entrypoint /bin/bash \\\n"
            "    --net=host --shm-size 512g \\\n"
            "    --device /dev/davinci0 ... --device /dev/davinci7 \\\n"
            "    -v /mnt/kvcache:/mnt/kvcache \\\n"
            "    {image}")
        self.custom_cmd_generate_btn = QPushButton("🔁 生成命令样例")
        self.custom_cmd_generate_btn.setEnabled(False)
        self.custom_cmd_generate_btn.setToolTip(
            "按当前配置（容器名/参数/挂载表）生成 Ascend 形态的命令样例，"
            "设备段为通用形态，请按实际集群修改。")
        self.custom_cmd_generate_btn.clicked.connect(
            self._generate_custom_command)
        gen_row = QHBoxLayout()
        gen_row.addWidget(self.custom_cmd_generate_btn)
        self.custom_cmd_note = QLabel(
            "命令中的 {image} 占位符执行时替换为所选镜像")
        gen_row.addWidget(self.custom_cmd_note, 0)
        gen_row.addStretch(1)
        hint_label = QLabel("自定义命令整条执行，跳过逐台设备适配；"
                            "容器名与上方「容器名」一致")
        hint_label.setProperty("role", "hint")
        # 默认收起（一屏友好）：勾选后才展开命令编辑区
        self.custom_cmd_widgets = [
            self.custom_cmd_edit, self.custom_cmd_generate_btn,
            self.custom_cmd_note, hint_label]
        for w in self.custom_cmd_widgets:
            w.setVisible(False)
        self.custom_cmd_check.toggled.connect(self.custom_cmd_edit.setEnabled)
        self.custom_cmd_check.toggled.connect(
            self.custom_cmd_generate_btn.setEnabled)
        # 两种命令模式互斥：勾选自定义命令后附加参数不再参与执行 -> 置灰提示
        self.custom_cmd_check.toggled.connect(self._on_custom_cmd_toggled)

        def _toggle_custom(show: bool) -> None:
            for w in self.custom_cmd_widgets:
                w.setVisible(show)

        self.custom_cmd_check.toggled.connect(_toggle_custom)
        lay.addWidget(self.custom_cmd_check)
        lay.addWidget(self.custom_cmd_edit)
        lay.addLayout(gen_row)
        lay.addWidget(hint_label)
        return page

    def _on_custom_cmd_toggled(self, on: bool) -> None:
        """勾选自定义 docker run 命令后，附加参数不再参与执行 -> 置灰提示。"""
        self.extra_args_edit.setEnabled(not on)
        self.extra_args_label.setEnabled(not on)
        self.extra_args_edit.setToolTip(
            "已启用「自定义 docker run 命令」，附加参数不再参与执行；\n"
            "需要附加参数请取消勾选自定义命令" if on else "")

    def _build_ucm_cfg_tab(self) -> QWidget:
        """「UCM文件配置」页签：容器创建完成后批量写入 UCM 配置文件。

        是否写入由右上角「创建容器并配置UCM文件」阶段开关决定
        （创建容器即写入）；本页签只负责配置内容与路径。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(4)

        hint = QLabel(
            "UCM 配置文件参考默认配置如下，按实际存储环境修改；"
            "是否写入由页签栏右上角「创建容器并配置UCM文件」开关决定"
            "（创建容器后即写入）。写入目标容器即「容器配置」页签中的容器名"
            "（全目标统一），写入路径须与步骤5 拉起命令的 "
            "<code>UCM_CONFIG_FILE</code> 一致。"
            "内容为空时保存模板按钮可恢复默认。")
        hint.setTextFormat(Qt.RichText)
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.ucm_cfg_view = QPlainTextEdit(UCM_CONFIG_TEMPLATE)
        self.ucm_cfg_view.setFont(font)
        self.ucm_cfg_view.setMinimumHeight(120)
        self.ucm_cfg_view.setToolTip(
            "UCM 参数配置文件内容（yaml）。可直接编辑，\n"
            "批量写入时以这里的当前内容为准。")
        attach_resize_grip(self.ucm_cfg_view)
        self.ucm_cfg_path_edit = QLineEdit(UCM_CONFIG_DEFAULT_PATH)
        self.ucm_cfg_path_edit.setFixedWidth(280)
        self.ucm_cfg_path_edit.setToolTip(
            "配置文件在各目标容器内的保存路径（须为绝对路径）。")
        self.ucm_cfg_restore_btn = QPushButton("🔁 恢复默认模板")
        self.ucm_cfg_restore_btn.setToolTip("把编辑框内容恢复为参考默认配置")
        self.ucm_cfg_restore_btn.clicked.connect(
            lambda: self.ucm_cfg_view.setPlainText(UCM_CONFIG_TEMPLATE))
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("容器内路径"))
        path_row.addWidget(self.ucm_cfg_path_edit)
        path_row.addSpacing(12)
        path_row.addWidget(self.ucm_cfg_restore_btn)
        path_row.addStretch(1)

        lay.addWidget(hint)
        lay.addWidget(self.ucm_cfg_view, 1)
        lay.addLayout(path_row)
        return page

    def _build_ucm_cfg_template(self) -> BatchUcmConfigConfig:
        """收集「UCM文件配置」页签参数（是否写入随创建容器阶段开关）。"""
        return BatchUcmConfigConfig(
            enabled=self.stage_container_check.isChecked(),
            content=self.ucm_cfg_view.toPlainText(),
            path=self.ucm_cfg_path_edit.text().strip())

    def _build_precheck_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(6)

        self.bandwidth_check = QCheckBox("执行存储带宽检查")
        self.bandwidth_check.setChecked(True)
        self.bandwidth_check.setToolTip(BANDWIDTH_TIP)
        self.mount_edit = QLineEdit()
        self.mount_edit.setPlaceholderText(
            "在「容器配置」添加 KV Cache 目录后，此处自动带出（可修改）")
        self._mount_dir_dirty = False     # 用户手动编辑过目录后停止自动带出
        self.mount_edit.setToolTip(
            "带宽测试目录（容器内路径）。须为容器创建参数中已映射进容器的目录，\n"
            "否则该服务器预检会报「挂载目录在容器内不存在」")
        row1 = QHBoxLayout()
        row1.addWidget(self.bandwidth_check)
        row1.addWidget(self.mount_edit, 1)
        lay.addLayout(row1)
        self.bandwidth_check.toggled.connect(self._on_bandwidth_toggled)
        # 用户手动编辑过带宽目录后不再自动带出 KV Cache 路径
        self.mount_edit.textEdited.connect(self._on_mount_dir_edited)

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
            '已预填常用配置模板，按需修改；JSON 只需写要覆盖的键')
        self.custom_cfg_edit.setFont(QFont("Consolas"))
        # 填满预检页签剩余高度：页签区域高度由内容最多的容器配置页签决定，
        # 固定高度会让本页签下方留出大片空白
        self.custom_cfg_edit.setMinimumHeight(84)
        self.custom_cfg_edit.setPlainText(DEFAULT_PRECHECK_TEMPLATE)
        self.custom_cfg_edit.textChanged.connect(self._validate_precheck_json)
        # 与容器配置「自定义 docker run 命令」同理：未勾选「使用自定义预检配置」
        # 时编辑框置灰（内容仍可见，作为参考模板），勾选后启用
        self.custom_cfg_edit.setEnabled(self.custom_cfg_check.isChecked())
        self.custom_cfg_check.toggled.connect(self.custom_cfg_edit.setEnabled)
        lay.addWidget(self.custom_cfg_edit, 1)
        self._update_bandwidth_state()
        return page

    def _build_run_box(self) -> QGroupBox:
        box = QGroupBox("3. 批量执行（进度与日志按服务器分页展示）")
        self.run_btn = QPushButton("🚀 开始批量部署")
        self.run_btn.setProperty("accent", True)
        self.run_btn.clicked.connect(self._run)
        # 动态说明：汇总各模块勾选的执行阶段与目标台数
        # （阶段开关分散在模块1/3 标题处，本区块只保留主操作）
        self.exec_summary_label = QLabel("")
        self.exec_summary_label.setProperty("role", "hint")

        exec_row = QHBoxLayout()
        # 主按钮靠右（与页面其他操作按钮位置一致），左侧放动态说明
        exec_row.addWidget(self.exec_summary_label, 1)
        exec_row.addWidget(self.run_btn)

        self.panel = ParallelTaskPanel()
        # 批量页定制任务面板高度：进度表至少可见 2 行、超过 ~8 行滚动查看；
        # 日志页签至少可见数行（通用面板默认 76px，多服务器日志只露 1-2 行）
        self.panel.table.setMinimumHeight(96)
        self.panel.table.setMaximumHeight(240)
        self.panel.tabs.setMinimumHeight(136)
        lay = QVBoxLayout(box)
        lay.setSpacing(6)
        lay.addLayout(exec_row)
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
            self.target_table.setSpan(0, 0, 1, 6)
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
            self.target_table.setItem(row, 5, self._make_env_item(server.id))
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

    def _make_env_item(self, server_id: str) -> QTableWidgetItem:
        """「环境」列单元格：步骤1 环境检测时宿主机检测的汇总状态
        （PASS 绿 / WARN 橙 / FAIL 红 / 未检测灰；悬浮显示各项明细）。"""
        env = self.ctx.env_results.get(server_id) or []
        summary = summarize_env_status(env)
        item = QTableWidgetItem(summary or "未探测")
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
            item.setToolTip("\n".join(
                f"[{it.status}] {it.name}  {it.summary}".rstrip()
                for it in env))
        return item

    def _on_mode_changed(self) -> None:
        pull_mode = self.mode_combo.currentIndex() == 1
        # 仓库拉取不经镜像包：无需源服务器，也不涉及目标保存路径
        self.source_combo.setEnabled(self.mode_combo.currentIndex() != 1)
        self.tar_remote_edit.setEnabled(not pull_mode)
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

    def _target_checkable_rows(self) -> int:
        """表格中可勾选的行数（空状态引导行不可勾选，不计入）。"""
        return sum(
            1 for row in range(self.target_table.rowCount())
            if self.target_table.item(row, 0) is not None
            and self.target_table.item(row, 0).flags() & Qt.ItemIsUserCheckable)

    def _on_target_row_clicked(self, row: int, col: int) -> None:
        """点击行任意位置 = 切换该台的部署勾选（第 0 列 checkbox 自带切换，
        避免重复翻转）——勾选不必瞄准小方框。"""
        if col == 0:
            return
        item = self.target_table.item(row, 0)
        if item is None or not (item.flags() & Qt.ItemIsUserCheckable):
            return    # 空状态引导行
        item.setCheckState(
            Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def _on_target_header_clicked(self, col: int) -> None:
        """点「部署」列表头 = 全选 / 清空（按当前是否已全部勾选取反）。"""
        if col != 0:
            return
        total = self._target_checkable_rows()
        all_checked = total > 0 and len(self._target_ids) >= total
        self._set_all_targets(not all_checked)

    def _show_target_menu(self, pos) -> None:
        """表格右键菜单：全选 / 清空 / 刷新列表（替代原顶部按钮行）。"""
        menu = QMenu(self)
        menu.addAction("全选", lambda: self._set_all_targets(True))
        menu.addAction("清空", lambda: self._set_all_targets(False))
        menu.addSeparator()
        menu.addAction("刷新列表", self.refresh_servers)
        menu.exec(self.target_table.viewport().mapToGlobal(pos))

    def _refresh_target_marks(self) -> None:
        """勾选行整行浅蓝高亮（覆盖隔行底色），与「已选 N 台」互相印证。"""
        for row in range(self.target_table.rowCount()):
            item0 = self.target_table.item(row, 0)
            if item0 is None or not (item0.flags() & Qt.ItemIsUserCheckable):
                continue    # 空状态引导行
            checked = item0.checkState() == Qt.Checked
            for col in range(1, 6):
                it = self.target_table.item(row, col)
                if it is not None:
                    # 取消勾选用空画刷清除高亮（不能用 QColor()——无效色会被
                    # 渲染成黑色）
                    it.setBackground(QBrush(QColor("#eaf2ff")) if checked
                                     else QBrush())

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
        self._refresh_target_marks()
        self._update_exec_summary()

    def _update_exec_summary(self) -> None:
        """模块4 的动态说明：汇总各模块勾选的执行阶段与目标台数。"""
        stages = []
        if self.stage_image_check.isChecked():
            stages.append("分发镜像")
        # 「创建容器并配置UCM文件」开关勾选 = 创建容器阶段开启，
        # 且每台容器创建完成后写入 UCM 配置（两步合一）
        if self.stage_container_check.isChecked():
            stages.append("创建容器并配置UCM文件")
        if self.stage_precheck_check.isChecked():
            stages.append("环境预检")
        n = len(self._target_ids)
        if not stages:
            text = "未勾选任何执行阶段（勾选模块2 右上角的阶段开关）"
        elif n == 0:
            text = "已选阶段：" + " → ".join(stages) + "（请先勾选目标服务器）"
        else:
            text = f"将对 {n} 台目标执行：" + " → ".join(stages)
        self.exec_summary_label.setText(text)

    @staticmethod
    def _image_exists_on(ssh, image_ref: str) -> bool:
        """目标侧镜像存在性检查（分发前自动检测，已有则跳过分发并在日志说明）。

        命令用 --format 形式，与源服务器侧 docker image inspect 可区分
        （exit 0 = 已存在）。"""
        res = ssh.exec(
            f"docker image inspect --format='{{{{.Id}}}}' {shq(image_ref)}",
            timeout=30)
        return getattr(res, "exit_code", 1) == 0

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
                if t_item is not None and t_item.text() == "模型":
                    QMessageBox.warning(self, "参数错误",
                                        "「模型」挂载最多一行（直接编辑已有「模型」行即可）")
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

    def _mount_paths(self) -> List[str]:
        """挂载表中全部非空宿主机路径（按行序去重）。"""
        paths: List[str] = []
        for row in range(self.mount_table.rowCount()):
            item = self.mount_table.item(row, 1)
            if item is None:
                continue
            path = item.text().strip()
            if path and path not in paths:
                paths.append(path)
        return paths

    def _mount_verify(self) -> None:
        """对全部勾选目标并行校验挂载路径是否存在（test -d）。"""
        paths = self._mount_paths()
        if not paths:
            QMessageBox.information(
                self, "提示", "挂载表为空——请先添加需要校验的宿主机路径")
            return
        servers = self._selected_targets()
        if not servers:
            QMessageBox.warning(
                self, "未选择目标服务器",
                "请先在「1. 目标服务器」勾选要校验的服务器")
            return

        def fn(ssh, tctx):
            ok_n = 0
            missing: List[str] = []
            total = len(paths)
            for i, path in enumerate(paths, 1):
                if tctx.cancelled():
                    raise RuntimeError("已取消")
                tctx.progress(int(100 * (i - 1) / max(total, 1)),
                              f"检查 {path}")
                res = ssh.exec(f"test -d {shq(path)}", timeout=15)
                if res.ok:
                    ok_n += 1
                    tctx.log(f"✓ 存在：{path}")
                else:
                    missing.append(path)
                    tctx.log(f"✗ 不存在：{path}")
            tctx.progress(100, f"校验完成（{ok_n}/{total} 存在）")
            if missing:
                tctx.log("缺失目录请在各服务器创建（mkdir -p）后重试，"
                         "或修改挂载表路径：")
                for path in missing:
                    tctx.log(f"    mkdir -p {path}")
            else:
                tctx.log("✓ 全部挂载路径在各目标服务器上均存在")

        self.panel.run_tasks(
            [(s, fn) for s in servers],
            f"批量校验挂载路径（{len(paths)} 个路径 × {len(servers)} 台）")

    # ---------------------------------------------------------- 右键菜单/远端挂载
    def _mount_menu(self, pos) -> None:
        """挂载表右键菜单：移除选中 / 批量校验。"""
        menu = QMenu(self)
        del_act = menu.addAction("🗑 移除选中")
        chk_act = menu.addAction("🔎 批量校验")
        act = menu.exec(self.mount_table.viewport().mapToGlobal(pos))
        if act is del_act:
            self._mount_del()
        elif act is chk_act:
            self._mount_verify()

    def _remote_mount(self) -> None:
        """打开「批量新增远端挂载」对话框（NFS over RDMA / DTFS）。"""
        servers = self._selected_targets()
        if not servers:
            QMessageBox.warning(
                self, "未选择目标服务器",
                "请先在「1. 目标服务器」勾选要在其宿主机上挂载的服务器")
            return
        dlg = RemoteMountDialog(
            self, title="批量新增远端挂载",
            ok_text=f"💾 在 {len(servers)} 台目标上挂载")
        dlg.mount_requested.connect(
            lambda: self._remote_mount_execute(dlg, servers))
        dlg.exec()

    def _remote_mount_execute(self, dlg, servers) -> None:
        """在全部勾选目标宿主机上并行执行挂载；成功后把目录加入映射表。"""
        err = dlg.validate()
        if err:
            QMessageBox.warning(dlg, "参数错误", err)
            return
        cmd = dlg.mount_command()
        d = dlg.local_dir()
        dlg.mount_btn.setEnabled(False)
        outcome: Dict[str, Tuple[bool, str]] = {}

        def make_fn(server):
            def fn(ssh, tctx):
                tctx.progress(5, f"创建挂载目录 {d}")
                res = ssh.exec(f"mkdir -p {shq(d)}", timeout=15)
                if not res.ok:
                    raise RuntimeError(
                        f"创建挂载目录失败：{res.stderr or res.stdout}")
                tctx.progress(30, "执行挂载命令")
                res = ssh.exec(cmd, timeout=60)
                if not res.ok:
                    raise RuntimeError(
                        f"挂载命令执行失败：\n{cmd}\n{res.stderr or res.stdout}")
                chk = ssh.exec(f"mountpoint -q {shq(d)}", timeout=15)
                if not chk.ok:
                    raise RuntimeError(
                        "挂载命令已执行，但 mountpoint -q 未检测到挂载点")
                outcome[server.id] = (True, "挂载成功")
                tctx.progress(100, "挂载完成")
            return fn

        def on_finished(all_ok: bool) -> None:
            dlg.mount_btn.setEnabled(True)
            failed = [s for s in servers
                      if outcome.get(s.id, (False, ""))[0] is False]
            if len(failed) == len(servers):
                QMessageBox.warning(dlg, "挂载失败",
                                    "全部目标服务器挂载失败，详情见各服务器日志页签")
                return
            mtype = dlg.table_type_combo.currentText()
            if mtype == "模型":
                for r in range(self.mount_table.rowCount()):
                    if self.mount_table.item(r, 0).text() == "模型":
                        QMessageBox.warning(
                            dlg, "无法加入映射表",
                            "「模型」类型仅允许一行（已存在），"
                            "可改用其他类型或手动编辑现有模型行")
                        return
            if mtype == "KV Cache":
                self._add_mount_row("KV Cache", d, d, False)
            elif mtype == "模型":
                self._add_mount_row("模型", d, "/models", True)
            else:
                self._add_mount_row("自定义", d, d, False)
            self.mount_table.scrollToBottom()
            if failed:
                names = "、".join((s.name or s.host) for s in failed)
                QMessageBox.warning(
                    dlg, "部分目标挂载失败",
                    f"以下服务器挂载失败（目录仍加入映射表，可批量校验确认）：\n{names}")
            dlg.accept()

        started = self.panel.run_tasks(
            [(s, make_fn(s)) for s in servers],
            f"批量挂载远端文件系统 {d}（{len(servers)} 台）",
            on_finished=on_finished)
        if not started:
            dlg.mount_btn.setEnabled(True)

    def _on_bandwidth_toggled(self, checked: bool) -> None:
        self.mount_edit.setEnabled(checked and self._has_kv_mount())

    def _on_mount_dir_edited(self, _text: str) -> None:
        self._mount_dir_dirty = True

    def _kv_mount_dirs(self) -> List[str]:
        """容器配置挂载表中的 KV Cache 目录（宿主机路径非空，按行序）。"""
        dirs: List[str] = []
        for row in range(self.mount_table.rowCount()):
            t_item = self.mount_table.item(row, 0)
            host = self.mount_table.item(row, 1)
            if (t_item is not None and t_item.text() == "KV Cache"
                    and host is not None and host.text().strip()):
                dirs.append(host.text().strip())
        return dirs

    def _has_kv_mount(self) -> bool:
        """容器配置挂载表中是否存在有效（宿主机路径非空）的 KV Cache 行。"""
        return bool(self._kv_mount_dirs())

    def _update_bandwidth_state(self) -> None:
        """带宽检查依赖 KV Cache 挂载：容器配置未添加目录时禁用（含勾选取消）。

        带宽测试目录默认带出第一个 KV Cache 路径（KV 行容器内路径与宿主机
        相同）；用户手动编辑过或填写的路径仍在 KV 目录集合中时不覆盖。
        """
        if not hasattr(self, "bandwidth_check"):
            return                     # 构造期：预检页签尚未创建
        kv_dirs = self._kv_mount_dirs()
        has_kv = bool(kv_dirs)
        self.bandwidth_check.setEnabled(has_kv)
        self.mount_edit.setEnabled(
            has_kv and self.bandwidth_check.isChecked())
        if not has_kv:
            self.bandwidth_check.setChecked(False)
            self.bandwidth_check.setToolTip(BANDWIDTH_TIP_NO_KV)
        else:
            self.bandwidth_check.setToolTip(BANDWIDTH_TIP)
            current = self.mount_edit.text().strip()
            if (not getattr(self, "_mount_dir_dirty", False)
                    and (not current or current not in kv_dirs)):
                self.mount_edit.setText(kv_dirs[0])

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
                continue                     # 空路径行跳过（不参与挂载）
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
        if self.custom_cmd_check.isChecked():
            cfg.custom_command = self.custom_cmd_edit.toPlainText().strip()
        cfg.validate()
        return cfg

    def _generate_custom_command(self) -> None:
        """按当前配置生成 Ascend 形态的自定义命令样例（设备段为通用形态）。"""
        name = self.name_edit.text().strip() or "ucm-vllm"
        shm = self.shm_edit.text().strip() or "512g"
        net = self.net_edit.text().strip() or "host"
        mounts = self._build_container_template()
        lines = [
            f"docker run -itd --name {name} --entrypoint /bin/bash",
            f"    --net={net} --shm-size {shm}",
            "    --device /dev/davinci0 ... --device /dev/davinci7",
            "    --device /dev/davinci_manager --device /dev/hisi_hdc",
            "    --device /dev/ummu --device /dev/uburma --privileged=true",
        ]
        for d in mounts.kv_cache_dirs:
            lines.append(f"    -v {d}:{d}")
        if mounts.model_mount is not None:
            ro = ":ro" if mounts.model_mount.readonly else ""
            lines.append(f"    -v {mounts.model_mount.host_path}:"
                         f"{mounts.model_mount.container_path}{ro}")
        for m in mounts.extra_mounts:
            ro = ":ro" if m.readonly else ""
            lines.append(f"    -v {m.host_path}:{m.container_path}{ro}")
        extra = self.extra_args_edit.toPlainText().strip()
        if extra:
            lines.append(f"    {extra}")
        lines.append("    {image}")
        self.custom_cmd_edit.setPlainText(" \\\n".join(lines))

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
            # 「创建容器并配置UCM文件」开关勾选 = 创建容器阶段开启
            # （创建容器后即写入 UCM 配置，两步合一）
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
                target_tar_dir=self.tar_remote_edit.text().strip() or "/tmp",
                recreate_container=self.recreate_check.isChecked(),
                servers=servers,
                container=self._build_container_template(),
                ucm_config=self._build_ucm_cfg_template(),
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
        self.tar_remote_edit.setText(tpl.target_tar_dir)
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
        # 统一挂载表回填：按类型逐行填入（默认空表）
        self.mount_table.setRowCount(0)
        for d in cfg.kv_cache_dirs:
            self._add_mount_row("KV Cache", d, d, False)
        if cfg.model_mount is not None:
            self._add_mount_row("模型", cfg.model_mount.host_path,
                                cfg.model_mount.container_path,
                                cfg.model_mount.readonly)
        for m in cfg.extra_mounts:
            self._add_mount_row("自定义", m.host_path, m.container_path, m.readonly)
        self.extra_args_edit.setPlainText(cfg.extra_args)
        # 自定义命令回填：模板含非空命令时勾选并填入；否则取消勾选
        has_custom = bool(cfg.custom_command.strip())
        self.custom_cmd_check.setChecked(has_custom)
        if has_custom:
            self.custom_cmd_edit.setPlainText(cfg.custom_command)

        ucm = tpl.ucm_config
        # 是否写入随「创建容器并配置UCM文件」阶段开关（导入旧模板时
        # ucm_config.enabled 仅作兼容，不再单独控制）
        if ucm.content.strip():
            self.ucm_cfg_view.setPlainText(ucm.content)
        self.ucm_cfg_path_edit.setText(ucm.path)

        pre = tpl.precheck
        self.bandwidth_check.setChecked(pre.bandwidth_check)
        self.mount_edit.setText(pre.mount_path)
        self.custom_cfg_check.setChecked(pre.custom_config_enabled)
        # 模板里没写自定义配置时保留默认模板内容
        if pre.custom_config_json.strip():
            self.custom_cfg_edit.setPlainText(pre.custom_config_json)
        self._validate_precheck_json()
        # 带宽检查可用性以容器配置的 KV Cache 挂载为准（回填后重新联动）
        self._update_bandwidth_state()

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
                                "请在「1. 目标服务器」勾选至少一台目标服务器")
            return
        image_ref = self.image_ref_edit.text().strip()
        try:
            validate_image_ref(image_ref)
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        if self.custom_cmd_check.isChecked() and not (
                self.custom_cmd_edit.toPlainText().strip()):
            QMessageBox.warning(
                self, "参数错误",
                "已勾选「自定义 docker run 命令」但命令为空；"
                "请填写命令或点「🔁 生成命令样例」后再修改，"
                "或取消勾选改用自动生成")
            return

        stages = self._build_stages()
        if not (stages.distribute_image or stages.create_container
                or stages.run_precheck):
            QMessageBox.warning(self, "参数错误", "请至少勾选一个执行阶段")
            return

        # 分发参数只在确实要分发镜像时校验；不分发时镜像须已在目标
        # 服务器（镜像名仍在「镜像与分发」页签填写，用于创建容器/预检）
        mode = ("tar", "pull", "direct")[self.mode_combo.currentIndex()]
        source = None
        if stages.distribute_image and mode in ("tar", "direct"):
            source = self._selected_source()
            if source is None:
                QMessageBox.warning(
                    self, "参数错误",
                    f"{('镜像包中转' if mode == 'tar' else '服务器直传')}方式需要选择源服务器")
                return
            remote_dir = self.tar_remote_edit.text().strip() or "/tmp"
            if not remote_dir.startswith("/"):
                QMessageBox.warning(
                    self, "参数错误",
                    f"目标保存路径必须是绝对路径（以 / 开头）: {remote_dir}")
                return

        try:
            container_tpl = self._build_container_template()
            ucm_tpl = self._build_ucm_cfg_template()
            precheck_tpl = self._build_precheck_template()
        except BatchDeployError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        if ucm_tpl.enabled and not stages.create_container:
            QMessageBox.warning(
                self, "参数错误",
                "「写入 UCM 配置文件」依赖容器创建阶段："
                "请勾选右上角「创建容器」阶段开关，或取消勾选写入开关")
            return

        # 重置共享状态
        self._reports = {}
        self._cleanup_local_tar()
        self._tar_ready = threading.Event()
        self._tar_error = ""
        self._src_tar = ""
        # 直传模式：源任务等待全部目标结束后清理源端镜像包（计数器而非
        # Event——面板要等所有任务结束才触发 on_finished，等它会死锁）
        self._targets_pending = len(servers)
        self._targets_lock = threading.Lock()

        tasks: List[Tuple[ServerInfo, object]] = []
        # tar/直传模式下源服务器先准备镜像包（docker save 很耗时；只创建
        # 容器/预检时无需准备，否则源服务器进度长期卡住）
        if mode in ("tar", "direct") and stages.distribute_image:
            tasks.append((source, self._make_prepare_task(
                source, image_ref, mode)))
        for server in servers:
            tasks.append((server, self._make_deploy_task(
                server, image_ref, mode, source, stages, container_tpl,
                ucm_tpl, precheck_tpl)))

        self.run_btn.setEnabled(False)
        title = (f"批量部署（{len(servers)} 台目标"
                 + ("，源服务器先准备镜像包" if mode in ("tar", "direct")
                    and stages.distribute_image else "")
                 + "）")
        if not self.panel.run_tasks(tasks, title, on_finished=self._on_finished):
            self.run_btn.setEnabled(True)
            self._cleanup_local_tar()

    def _make_prepare_task(self, source: ServerInfo, image_ref: str,
                           mode: str):
        """源服务器任务：docker save 导出镜像包。

        - tar：导出后下载到本机中转，删除源端临时包；
        - direct：导出后等待各目标 scp 拉取完成，最后清理源端包。
        """
        uid = uuid.uuid4().hex[:8]
        remote = f"/tmp/ucm-batch-{uid}.tar"
        local = os.path.join(tempfile.gettempdir(), f"ucm-batch-{uid}.tar")
        if mode == "tar":
            self._local_tar = local
        else:
            self._src_tar = remote

        def fn(ssh, tctx):
            try:
                tctx.progress(5, "docker save 导出镜像（可能需要数分钟）...")
                export_image_tar(ssh, DockerManager(ssh), image_ref, remote,
                                 log=tctx.log, cancelled=tctx.cancelled)
                if mode == "direct":
                    tctx.progress(100, "镜像包已导出（等待各目标服务器拉取）")
                else:
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

            if mode == "direct":
                # 等待全部目标流水线结束（成功/失败均会计数），随后清理源端包
                while True:
                    with self._targets_lock:
                        if self._targets_pending <= 0:
                            break
                    if tctx.cancelled():
                        return
                    time.sleep(0.2)
                if self._tar_error:
                    return          # 源包可能仍需要人工排查，不自动删除
                try:
                    tctx.log(f"$ rm -f {remote}（清理源端镜像包）")
                    ssh.exec(f"rm -f {shq(remote)}", timeout=60)
                except Exception:
                    tctx.log(f"源端镜像包清理失败，请手动删除: {remote}")

        return fn

    def _make_deploy_task(self, server: ServerInfo, image_ref: str, mode: str,
                          source: Optional[ServerInfo], stages: BatchStages,
                          container_tpl: BatchContainerConfig,
                          ucm_tpl: BatchUcmConfigConfig,
                          precheck_tpl: BatchPrecheckConfig):
        """一台目标服务器的流水线任务：分发 -> 创建容器 -> 预检。"""
        ranges = stage_progress_ranges(stages)
        recreate = self.recreate_check.isChecked()
        remote_dir = self.tar_remote_edit.text().strip() or "/tmp"
        report = BatchTargetReport(server_id=server.id,
                                   server_name=server.name, image=image_ref)

        def fn(ssh, tctx):
            def stage_progress(stage: str, pct: int, msg: str) -> None:
                lo, hi = ranges.get(stage, (0, 100))
                pct = max(0, min(100, int(pct)))
                tctx.progress(lo + int((hi - lo) * pct / 100), msg)

            docker = DockerManager(ssh)

            try:
                # ---- 阶段1：镜像分发（目标已有同名镜像时自动跳过并在日志说明：
                #      分发目标就是让镜像就位，已存在即视为分发成功）
                if stages.distribute_image:
                    stage_progress("image", 2, "准备镜像...")
                    if self._image_exists_on(ssh, image_ref):
                        report.image_skipped = True
                        tctx.log(f"目标已存在镜像 {image_ref}，跳过分发")
                        stage_progress("image", 100, "镜像已存在，跳过分发")
                    elif mode == "pull":
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
                        if mode == "direct":
                            dst = (remote_dir.rstrip("/") + "/"
                                   + os.path.basename(self._src_tar))
                            scp_image_between(
                                ssh, source, self._src_tar, dst,
                                progress=lambda p, m: stage_progress("image", p, m),
                                log=tctx.log, cancelled=tctx.cancelled)
                            load_remote_tar(
                                ssh, docker, dst, image_ref,
                                progress=lambda p, m: stage_progress("image", p, m),
                                log=tctx.log)
                        else:
                            load_image_tar(
                                ssh, docker, self._local_tar, image_ref,
                                progress=lambda p, m: stage_progress("image", p, m),
                                log=tctx.log, cancelled=tctx.cancelled,
                                remote_dir=remote_dir)

                # ---- 阶段2：容器创建
                if stages.create_container:
                    name, _cmd = create_container_from_template(
                        ssh, docker, container_tpl, image_ref,
                        progress=lambda p, m: stage_progress("container", p, m),
                        log=tctx.log, recreate=recreate,
                        cancelled=tctx.cancelled)
                    report.container = name
                    # 容器创建完成后按「UCM文件配置」页签写入配置文件
                    # （进度归入容器创建阶段尾部；失败则该台部署失败）
                    write_ucm_config_on_target(
                        ssh, ucm_tpl, report.container or container_tpl.name,
                        progress=lambda p, m: stage_progress("container", p, m),
                        log=tctx.log)

                # ---- 阶段3：环境预检
                if stages.run_precheck:
                    container = report.container or container_tpl.name
                    tctx.log(f"预检目标容器: {container}")
                    result = run_precheck_on_target(
                        ssh, precheck_tpl, container,
                        progress=lambda p, m: stage_progress("precheck", p, m),
                        log=tctx.log, cancelled=tctx.cancelled)
                    report.precheck_summary = result.summary
                    report.precheck_verdict = result.verdict
            except Exception as exc:
                # 失败目标也记录报告：汇总弹窗能展示每台结果；
                # 完整失败原因写入日志页签，状态栏/弹窗只提示查看日志
                report.error = str(exc)
                self._reports[server.id] = report
                tctx.log(f"[deployer] ✗ 执行失败: {exc}")
                with self._targets_lock:
                    self._targets_pending -= 1
                if "取消" in str(exc):
                    raise
                raise BatchDeployError(
                    "批量部署失败（详见该服务器日志页签）") from exc

            self._reports[server.id] = report
            tctx.progress(100, report.describe())
            with self._targets_lock:
                self._targets_pending -= 1

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
                          if r.precheck_verdict == "failed" or r.error]
        for server in self._selected_targets():
            rep = self._reports.get(server.id)
            if rep is None:
                continue
            icon = ("✗" if (rep.precheck_verdict == "failed" or rep.error)
                    else "✓")
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
