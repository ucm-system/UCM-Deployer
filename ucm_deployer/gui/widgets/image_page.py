# -*- coding: utf-8 -*-
"""步骤2：镜像构建 —— 在已登录服务器上构建带 UCM 的镜像。

流程：选择基础镜像（docker images / 上传 tar）
      -> 上传 wrapt whl、UCM whl、（可选）ucm-toolkit whl
      -> 校验服务器/镜像/安装包匹配 -> 填新镜像名 -> 构建并校验 UCM 安装。
"""
from __future__ import annotations

import os
import shlex
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.docker_manager import (DockerManager, platform_from_arch,
                                    validate_image_ref)
from ...core.image_builder import (ImageBuildConfig, ImageBuilder,
                                   ImageRuntimeInfo,
                                   expected_ucm_components,
                                   fetch_image_runtime_info,
                                   fetch_server_arch, suggest_tag,
                                   ucm_wheel_components,
                                   upload_image_tar, validate_build_match,
                                   validate_whl, wheel_distribution,
                                   whl_arch, whl_glibc, whl_python_abi,
                                   whl_version)
from ...core.models import DeviceType, DockerImage, UCMInfo
from ...core.shell import shq
from ..state import AppContext
from .common import (ParallelTaskPanel, QuickTask, combo_ref, fill_image_combo,
                     image_from_ref, setup_image_combo)


class _FitStack(QStackedWidget):
    """高度跟随当前页的堆栈（QStackedWidget 默认按最高页预留，产生大片留白）。"""

    def sizeHint(self):
        return self.currentWidget().sizeHint()

    def minimumSizeHint(self):
        return self.currentWidget().minimumSizeHint()


class ImagePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._images: List[DockerImage] = []
        self._loaded_sid: Optional[str] = None
        self._quick_tasks = []      # 运行中的 QuickTask 引用（防线程被回收）
        self._tag_dirty = False     # 用户手动编辑过新镜像名（停止自动建议）
        self._updating_tag = False  # 程序设置 tag 中（不算用户编辑）
        self._pull_command_dirty = False
        self._updating_pull_command = False
        self._image_runtime: Optional[ImageRuntimeInfo] = None
        self._runtime_cache = {}
        self._ucm_cache = {}        # ref -> UCMInfo（扫描/底座检查结果，避免重复 SSH）
        self._scan_installed = []   # [(ref, UCMInfo)] 最近一次扫描发现的已装 UCM 镜像

        # ---------- 1. UCM 镜像检查（自动扫描服务器镜像，只给结论提示；
        #              不再放镜像下拉——选 UCM 镜像创建容器是步骤3 的职责）
        scan_box = QGroupBox("UCM 镜像检查")
        self.placeholder = QLabel("请先在「1. 服务器管理」点选要登录的服务器")
        self.placeholder.setProperty("role", "hint")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setMinimumHeight(48)
        self.scan_state_label = QLabel("镜像 UCM：进入本页后自动扫描")
        self.scan_state_label.setWordWrap(True)
        scan_layout = QVBoxLayout(scan_box)
        scan_layout.addWidget(self.placeholder)
        scan_layout.addWidget(self.scan_state_label)

        # ---------- 2. 构建新 UCM 镜像（常驻；基础镜像是构建的输入配置）
        self.image_combo = QComboBox()
        setup_image_combo(self.image_combo)
        self.image_combo.setMinimumWidth(440)
        self.image_combo.currentTextChanged.connect(self._suggest_tag_if_empty)

        self.source_combo = QComboBox()
        self.source_combo.addItem("服务器已有镜像")
        self.source_combo.addItem("本地镜像包（离线导入）")
        self.source_combo.addItem("镜像仓库（服务器拉取）")
        self.source_combo.currentIndexChanged.connect(
            lambda *_: self._update_source_hint())

        self.source_stack = _FitStack()
        self.source_combo.currentIndexChanged.connect(
            self.source_stack.setCurrentIndex)

        existing_page = QWidget()
        existing_form = QFormLayout(existing_page)
        refresh_btn = QPushButton("🔄 重新扫描")
        combo_row = QHBoxLayout()
        combo_row.addWidget(self.image_combo, 1)
        combo_row.addWidget(refresh_btn)
        existing_form.addRow("基础镜像", combo_row)
        self.source_stack.addWidget(existing_page)

        tar_page = QWidget()
        tar_form = QFormLayout(tar_page)
        self.tar_path_edit = QLineEdit()
        self.tar_path_edit.setPlaceholderText(
            "选择本地镜像 tar / tar.gz / tgz 文件；仅在服务器无法访问仓库时使用")
        self.tar_remote_edit = QLineEdit()
        self.tar_remote_edit.setText("/tmp")
        self.tar_remote_edit.setPlaceholderText("/tmp")
        self.tar_remote_edit.setToolTip(
            "镜像包在服务器上的保存目录（绝对路径，默认 /tmp）。\n"
            "大镜像包建议改到大容量目录（如 /data），避免 /tmp 空间不足。")
        browse_tar_btn = QPushButton("浏览…")
        upload_btn = QPushButton("导入到服务器")
        browse_tar_btn.clicked.connect(
            lambda: self._pick_file(self.tar_path_edit, "镜像包 (*.tar *.tar.gz *.tgz)"))
        upload_btn.clicked.connect(self._upload_tar)
        tar_btns = QHBoxLayout()
        tar_btns.addWidget(browse_tar_btn)
        tar_btns.addWidget(upload_btn)
        tar_btns.addStretch(1)
        tar_form.addRow("镜像包", self.tar_path_edit)
        tar_form.addRow("服务器保存路径", self.tar_remote_edit)
        tar_form.addRow("", tar_btns)
        self.source_stack.addWidget(tar_page)

        registry_page = QWidget()
        registry_form = QFormLayout(registry_page)
        self.registry_ref_edit = QLineEdit()
        self.registry_ref_edit.setPlaceholderText(
            "quay.io/ascend/vllm-ascend:v0.30.0rc2-a5-openeuler")
        self.registry_ref_edit.setMinimumWidth(520)
        self.pull_command_edit = QPlainTextEdit()
        self.pull_command_edit.setPlaceholderText(
            "输入镜像地址后自动生成 docker pull 命令；可在服务器上执行前修改")
        self.pull_command_edit.setMinimumHeight(76)
        self.pull_command_edit.setToolTip(
            "命令将在目标服务器上执行；执行前可编辑，但必须以 docker pull 开头")
        pull_btn = QPushButton("在服务器上拉取")
        pull_btn.clicked.connect(self._pull_registry_image)
        registry_btns = QHBoxLayout()
        registry_btns.addWidget(pull_btn)
        registry_btns.addStretch(1)
        registry_form.addRow("镜像地址", self.registry_ref_edit)
        registry_form.addRow("", registry_btns)
        registry_form.addRow("服务器命令（可编辑）", self.pull_command_edit)
        self.source_stack.addWidget(registry_page)
        self.registry_ref_edit.textChanged.connect(self._update_pull_command)
        self.pull_command_edit.textChanged.connect(self._mark_pull_command_dirty)

        refresh_btn.setToolTip("重新扫描服务器镜像并检查 UCM")
        upload_btn.setToolTip("上传本地镜像包到服务器并执行 docker load")
        pull_btn.setToolTip(
            "在目标服务器上直接执行 docker pull；本地无需安装 Docker Desktop")

        # ---------- 2a. 基础镜像（构建的输入配置；运行时探测用于安装包匹配提示）
        base_box = QGroupBox("基础镜像（构建底座）")
        self.runtime_label = QLabel("基础镜像运行时：选择镜像后自动检测")
        self.runtime_label.setProperty("role", "hint")
        self.runtime_label.setWordWrap(True)
        base_layout = QVBoxLayout(base_box)
        source_form = QFormLayout()
        source_form.addRow("镜像来源", self.source_combo)
        base_layout.addLayout(source_form)
        base_layout.addWidget(self.source_stack)
        self.source_hint = QLabel()
        self.source_hint.setWordWrap(True)
        base_layout.addWidget(self.source_hint)
        base_layout.addWidget(self.runtime_label)
        self._update_source_hint()
        self._update_pull_command()

        # ---------- 2b. 安装包（常驻；仅构建新 UCM 镜像时才需要填写）
        self.build_box = QGroupBox("安装包（仅构建新 UCM 镜像时填写）")
        self.wrapt_whl_edit = QLineEdit()
        self.ucm_whl_edit = QLineEdit()
        self.toolkit_edit = QLineEdit()
        for w in (self.wrapt_whl_edit, self.ucm_whl_edit, self.toolkit_edit):
            w.setPlaceholderText("本地文件路径")
        wrapt_btn = QPushButton("浏览...")
        ucm_btn = QPushButton("浏览...")
        toolkit_btn = QPushButton("浏览...")
        wrapt_btn.clicked.connect(lambda: self._pick_file(self.wrapt_whl_edit, "wrapt whl (*.whl)"))
        ucm_btn.clicked.connect(lambda: self._pick_file(self.ucm_whl_edit, "UCM whl (*.whl)"))
        toolkit_btn.clicked.connect(
            lambda: self._pick_file(self.toolkit_edit, "ucm-toolkit whl (*.whl)"))

        # 在线下载提示（本地没有包时指引获取途径，链接可点击直达浏览器）
        self.ucm_dl_hint = QLabel(
            '⬇ 本地没有 UCM 包？<a href="https://github.com/ModelEngine-Group/'
            'unified-cache-management/releases">从 GitHub Releases 在线下载'
            ' uc_manager-*.whl</a>（按基础镜像选择 CANN/CUDA、A2/A3/A5、'
            'cp311/cp312 与 CPU 架构）')
        self.wrapt_dl_hint = QLabel(
            '⬇ 离线依赖 wrapt：<a href="https://pypi.org/project/wrapt/1.17.2/#files">'
            '从 PyPI (wrapt 1.17.2) 文件列表在线下载</a>'
            '（选择匹配基础镜像 Python 版本、CPU 架构与 manylinux/glibc 的 whl）')
        self.toolkit_dl_hint = QLabel(
            '⬇ 本地没有 ucm-toolkit 包？<a href="https://github.com/ModelEngine-Group/'
            'unified-cache-management/releases">从 GitHub Releases 在线下载</a>'
            '（与 UCM 包同站点，pip install 安装）')
        for hint in (self.ucm_dl_hint, self.wrapt_dl_hint, self.toolkit_dl_hint):
            hint.setProperty("role", "hint")
            hint.setOpenExternalLinks(True)
        self.ucm_dl_hint.setToolTip(
            "https://github.com/ModelEngine-Group/unified-cache-management/releases")
        self.wrapt_dl_hint.setToolTip("https://pypi.org/project/wrapt/1.17.2/#files")
        self.toolkit_dl_hint.setToolTip(
            "https://github.com/ModelEngine-Group/unified-cache-management/releases")

        # whl 选择即时反馈：文件有效性 + 架构标记解析（帮助确认与服务器匹配）
        self.wrapt_fb = QLabel("")
        self.ucm_fb = QLabel("")
        self.toolkit_fb = QLabel("")
        for fb in (self.wrapt_fb, self.ucm_fb, self.toolkit_fb):
            fb.setProperty("role", "hint")
            fb.setVisible(False)   # 未填写路径时隐藏，避免空行留白
        self.wrapt_whl_edit.textChanged.connect(
            lambda t: self._update_whl_feedback(self.wrapt_fb, t, "wrapt"))
        self.ucm_whl_edit.textChanged.connect(
            lambda t: (self._update_whl_feedback(self.ucm_fb, t, "uc-manager"),
                       self._suggest_tag_if_empty()))   # UCM 版本号影响建议名
        self.toolkit_edit.textChanged.connect(
            lambda t: self._update_whl_feedback(self.toolkit_fb, t, "ucm-toolkit"))

        self.image_combo.currentTextChanged.connect(self._on_base_image_changed)

        form = QFormLayout(self.build_box)
        row_w = QHBoxLayout()
        row_w.addWidget(self.wrapt_whl_edit, 1)
        row_w.addWidget(wrapt_btn)
        row_u = QHBoxLayout()
        row_u.addWidget(self.ucm_whl_edit, 1)
        row_u.addWidget(ucm_btn)
        row_t = QHBoxLayout()
        row_t.addWidget(self.toolkit_edit, 1)
        row_t.addWidget(toolkit_btn)
        form.addRow("wrapt whl 包*", row_w)
        form.addRow(self.wrapt_fb)
        form.addRow(self.wrapt_dl_hint)
        form.addRow("UCM whl 包*", row_u)
        form.addRow(self.ucm_fb)
        form.addRow(self.ucm_dl_hint)
        form.addRow("ucm-toolkit whl 包（可选）", row_t)
        form.addRow(self.toolkit_fb)
        form.addRow(self.toolkit_dl_hint)

        # ---------- 3. 新镜像名与构建操作（仅「构建新 UCM 镜像」分支显示）
        name_box = QGroupBox("新镜像名与构建")
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("默认自动生成：<基础镜像名>:ucm-<UCM版本号>，可修改")
        self.tag_edit.textChanged.connect(self._on_tag_edited)
        self.platform_label = QLabel("-（进入本页后按服务器设备自动确定）")
        name_form = QFormLayout(name_box)
        name_form.addRow("新镜像名", self.tag_edit)
        name_form.addRow("构建平台(ENV PLATFORM)", self.platform_label)

        # ---------- 构建操作
        self.match_label = QLabel("匹配校验：未校验（构建前会自动强制校验）")
        self.match_label.setProperty("role", "hint")
        self.match_label.setWordWrap(True)
        match_btn = QPushButton("🔍 校验匹配（服务器/镜像/安装包）")
        build_btn = QPushButton("🔨 开始构建")
        build_btn.setProperty("accent", True)
        match_btn.clicked.connect(self._check_match)
        build_btn.clicked.connect(self._build)
        build_btns = QHBoxLayout()
        build_btns.addWidget(match_btn)
        build_btns.addWidget(build_btn)
        build_btns.addStretch(1)
        build_area_layout = QVBoxLayout()
        build_area_layout.setContentsMargins(0, 0, 0, 0)
        build_area_layout.addWidget(base_box)
        build_area_layout.addWidget(self.build_box)
        build_area_layout.addWidget(name_box)
        build_area_layout.addWidget(self.match_label)
        build_area_layout.addLayout(build_btns)
        self.build_area = QWidget()
        self.build_area.setLayout(build_area_layout)

        self.hint = QLabel(
            "进入本页会自动扫描服务器镜像并检查是否已安装 UCM：发现已含 UCM 的镜像时"
            "本步骤可跳过，直接进入步骤3创建容器，无需填写安装包或构建；\n"
            "未发现且确需制作新 UCM 镜像时，在下方选择基础镜像、填写安装包后构建："
            "生成 Dockerfile -> docker build -> 校验 UCM 安装。")
        self.hint.setWordWrap(True)

        self.panel = ParallelTaskPanel()

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(scan_box)
        content_layout.addWidget(self.build_area)
        content_layout.addWidget(self.hint)

        # 内容区（上）/ 任务记录（下）可拖动分栏；任务记录常驻显示
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(content)
        self.splitter.addWidget(self.panel)
        self.splitter.setStretchFactor(0, 7)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.splitter)

    def _set_scan_state(self, text: str, color: str) -> None:
        self.scan_state_label.setText(text)
        self.scan_state_label.setStyleSheet(f"color:{color};")

    # ------------------------------------------------------------ 初始化
    def on_enter(self) -> None:
        """进入本页：扫描服务器镜像并检查 UCM（有缓存则回显）。"""
        server = self.ctx.server
        self.placeholder.setVisible(server is None)
        self._update_platform_label()
        if server is None:
            return
        if self._loaded_sid != server.id:
            self._reset_for_server_switch()
            self._scan_images()
        else:
            self._apply_images()

    def _reset_for_server_switch(self) -> None:
        """切换服务器：清理上一台的镜像列表/UCM 与运行时缓存、复位控件。

        不清理的话，新服务器扫描失败时界面仍显示上一台的镜像，误导用户。
        """
        self._images = []
        self._loaded_sid = None
        self._ucm_cache = {}
        self._runtime_cache = {}
        self._scan_installed = []
        self._image_runtime = None
        fill_image_combo(self.image_combo, [])
        self.runtime_label.setText("基础镜像运行时：选择镜像后自动检测")
        self._refresh_whl_feedback()

    def _update_platform_label(self) -> None:
        server = self.ctx.server
        dev = self.ctx.device_of(server) if server else None
        if dev is not None and dev.device_type == DeviceType.ASCEND:
            self.platform_label.setText("ascend（服务器设备：Ascend NPU）")
        elif dev is not None and dev.device_type == DeviceType.NVIDIA:
            self.platform_label.setText("cuda（服务器设备：NVIDIA GPU）")
        else:
            self.platform_label.setText("-（未探测设备，默认按 ascend；建议先在步骤1探测）")

    def _update_source_hint(self) -> None:
        hints = {
            0: "已选择服务器已有镜像：只需从列表选择并检查 UCM，无需填写仓库地址或上传镜像包。",
            1: "离线导入：选择本地 tar/tar.gz 镜像包，上传到服务器并 docker load；适合服务器无法访问镜像仓库。",
            2: "服务器直连拉取：在目标服务器上执行 docker pull，本地不需要安装 Docker Desktop。",
        }
        self.source_hint.setText(hints.get(self.source_combo.currentIndex(), ""))

    def _update_pull_command(self) -> None:
        if self._updating_pull_command:
            return
        server = self.ctx.server
        ref = self.registry_ref_edit.text().strip()
        if not ref:
            self._set_pull_command("")
            return
        platform = platform_from_arch(self.ctx.device_of(server).arch) if server else ""
        command = "docker pull"
        if platform:
            command += f" --platform {shq(platform)}"
        command += f" {shq(ref)}"
        self._set_pull_command(command)

    def _set_pull_command(self, command: str) -> None:
        self._updating_pull_command = True
        try:
            self.pull_command_edit.setPlainText(command)
            self._pull_command_dirty = False
        finally:
            self._updating_pull_command = False

    def _mark_pull_command_dirty(self) -> None:
        if not self._updating_pull_command:
            self._pull_command_dirty = True

    def _select_source(self, index: int) -> None:
        self.source_combo.setCurrentIndex(index)
        if index == 0:
            self._on_base_image_changed(combo_ref(self.image_combo))

    def _selected_base(self) -> Optional[str]:
        if self.source_combo.currentIndex() != 0:
            QMessageBox.information(
                self, "请先完成镜像准备",
                "请先导入本地镜像包或在服务器上拉取镜像。\n"
                "操作成功后会自动切换回“服务器已有镜像”，再检查 UCM。")
            return None
        ref = combo_ref(self.image_combo)
        if not ref:
            QMessageBox.warning(
                self, "参数错误",
                "未选择基础镜像。\n"
                "镜像列表来自服务器 docker images（进入本页自动加载，"
                "也可点「刷新镜像列表」），只能从列表中选择。")
            return None
        return ref

    def _suggest_tag_if_empty(self, *_):
        """镜像/UCM whl 变化时刷新建议名；用户手动编辑过则不覆盖。"""
        if self._tag_dirty:
            return
        ref = combo_ref(self.image_combo)
        if not ref:
            return
        ucm_ver = whl_version(self.ucm_whl_edit.text().strip())
        self._updating_tag = True
        self.tag_edit.setText(suggest_tag(ref, ucm_ver))
        self._updating_tag = False

    def _on_base_image_changed(self, ref: str) -> None:
        """选择基础镜像后自动探测运行时并精确检查该镜像的 UCM 安装：

        - 运行时 -> 安装包匹配建议与强校验；
        - UCM 结果 -> 状态行引导（已装可直接用作 UCM 镜像，无需构建）。
        扫描按名称快筛可能漏报，这里是用户显式选中镜像的精确兜底。
        """
        self._image_runtime = None
        self._refresh_whl_feedback()
        server = self.ctx.server
        ref = (ref or "").strip()
        if not ref or server is None or self.source_combo.currentIndex() != 0:
            self.runtime_label.setText(
                "基础镜像运行时：选择服务器已有镜像后自动检测")
            return
        cached_ucm = self._ucm_cache.get(ref)
        if cached_ucm is not None:
            # 扫描已精确检查过该镜像：立即应用结论（同步），不重复 SSH
            self._apply_base_ucm_state(ref, cached_ucm)
        if ref in self._runtime_cache:
            self._show_runtime_info(ref, self._runtime_cache[ref])
            return
        self.runtime_label.setText(f"基础镜像运行时：正在检测 {ref} ...")

        def fn():
            from ...core.ssh_client import SSHClient
            with SSHClient(server) as ssh:
                info = (self._runtime_cache.get(ref)
                        or fetch_image_runtime_info(ssh, ref))
                ucm = cached_ucm or DockerManager(ssh).image_ucm_info(ref)
                return info, ucm

        def on_done(result, error):
            if combo_ref(self.image_combo) != ref:
                return
            if error is not None:
                self.runtime_label.setText(
                    f"基础镜像运行时：检测失败（{error}）。"
                    "构建前仍会尝试校验，请手动核对 whl。")
                return
            info, ucm = result
            self._runtime_cache[ref] = info
            if cached_ucm is None:
                # 只有本次真正做了 UCM 检查才更新结论，避免覆盖扫描文案
                self._ucm_cache[ref] = ucm
                self._apply_base_ucm_state(ref, ucm)
            self._show_runtime_info(ref, info)

        self._run_quick(fn, on_done)

    def _apply_base_ucm_state(self, ref: str, ucm: UCMInfo) -> None:
        """底座检查结论（比全局扫描更贴近当前操作，覆盖显示）。"""
        if ucm.installed:
            if self.ctx.server is not None:
                self.ctx.images[self.ctx.server.id] = ref
            self._set_scan_state(
                f"✓ 基础镜像 {ref} 已安装 UCM {ucm.version}——"
                "无需构建，可直接进入步骤3使用；也可在下方按需构建新镜像",
                "#16a34a")
        else:
            self._set_scan_state(
                f"基础镜像 {ref} 未安装 UCM——如需使用，"
                "请在下方填写安装包并构建", "#d97706")

    def _show_runtime_info(self, ref: str, info: ImageRuntimeInfo) -> None:
        self._image_runtime = info
        device = self.ctx.device_of(self.ctx.server) if self.ctx.server else None
        platform, runtime_code, variant = expected_ucm_components(
            device, ref, info)
        if platform == "cuda":
            ucm_name = "uc_manager_cuda"
            if runtime_code:
                ucm_name += f"_cu{runtime_code}"
        else:
            ucm_name = "uc_manager"
            if runtime_code:
                ucm_name += f"_cann{runtime_code}"
            if variant:
                ucm_name += f"_{variant}"
        if info.python_abi:
            ucm_name += (
                f"-*-{info.python_abi}-{info.python_abi}"
                f"-manylinux_2_{info.ucm_wheel_glibc.split('.')[1]}"
            )
            if info.arch:
                ucm_name += f"_{info.arch}"
            ucm_name += ".whl"
        else:
            ucm_name += "-*.whl"

        wrapt_name = "wrapt-1.17.2"
        if info.python_abi and info.arch:
            wrapt_name += (
                f"-{info.python_abi}-{info.python_abi}"
                f"-manylinux_2_17_{info.arch}.manylinux2014_{info.arch}.whl"
            )
        elif info.arch:
            wrapt_name += f"-*-manylinux_2_17_{info.arch}.whl"
        else:
            wrapt_name += "-*.whl"

        runtime_text = info.summary or "未能从镜像内探测到运行时细节"
        if not info.probe_ok:
            runtime_text += "（探测不完整）"
        self.runtime_label.setText(
            f"基础镜像运行时：{runtime_text}\n"
            f"建议 UCM：{ucm_name}\n"
            f"建议 wrapt：{wrapt_name}")
        self._refresh_whl_feedback()

    def _on_tag_edited(self, text: str):
        """区分用户编辑与程序设置：用户改过即停止自动建议。"""
        if not self._updating_tag and text.strip():
            self._tag_dirty = True

    def _refresh_whl_feedback(self) -> None:
        self._update_whl_feedback(
            self.wrapt_fb, self.wrapt_whl_edit.text(), "wrapt")
        self._update_whl_feedback(
            self.ucm_fb, self.ucm_whl_edit.text(), "uc-manager")
        self._update_whl_feedback(
            self.toolkit_fb, self.toolkit_edit.text(), "ucm-toolkit")

    def _update_whl_feedback(self, label: QLabel, text: str,
                             expected_distribution: str) -> None:
        """whl 路径即时反馈：文件有效性 + 基础镜像运行时匹配（空路径时隐藏行）。"""
        path = text.strip()
        if not path:
            label.setText("")
            label.setVisible(False)
            return
        label.setVisible(True)
        name = os.path.basename(path)
        if not os.path.isfile(path):
            label.setText(f"⚠ 文件不存在: {name}")
            return

        facts = [f"文件有效 · 包名 {wheel_distribution(path) or '-'}"]
        abi = whl_python_abi(path)
        facts.append(f"Python {abi or '通用'}")
        arch = whl_arch(path)
        facts.append(f"架构 {arch or '通用'}")
        glibc = whl_glibc(path)
        if glibc:
            facts.append(f"glibc ≥ {glibc}")

        errors, warnings = validate_whl(
            path, expected_distribution, runtime=self._image_runtime,
            fallback_arch=(self.ctx.device_of(self.ctx.server).arch
                           if self.ctx.server else ""))
        if expected_distribution == "uc-manager" and self.ctx.server:
            wheel_platform, wheel_runtime, wheel_variant = \
                ucm_wheel_components(path)
            expected_platform, expected_runtime, expected_variant = \
                expected_ucm_components(
                    self.ctx.device_of(self.ctx.server),
                    combo_ref(self.image_combo), self._image_runtime)
            if wheel_platform in ("cann", "cuda") and \
                    wheel_platform != expected_platform:
                errors.append(f"UCM 平台应为 {expected_platform}")
            elif wheel_platform == "generic" and expected_platform:
                warnings.append("通用 UCM 包，请确认支持当前 CANN/CUDA 与设备变体")
            if (wheel_runtime and expected_runtime
                    and wheel_runtime != expected_runtime):
                errors.append(f"UCM runtime 应为 {expected_runtime}")
            if (wheel_variant and expected_variant
                    and wheel_variant != expected_variant):
                errors.append(f"UCM 设备变体应为 {expected_variant.upper()}")

        if errors:
            label.setText("⚠ " + " · ".join(facts) + "\n" + "；".join(errors))
        elif warnings:
            label.setText("⚠ " + " · ".join(facts) + "\n" + "；".join(warnings))
        elif self._image_runtime is not None:
            label.setText("✓ " + " · ".join(facts) + " · 与基础镜像运行时匹配")
        else:
            label.setText("✓ " + " · ".join(facts) + " · 等待基础镜像运行时检测")

    def _pick_file(self, edit: QLineEdit, flt: str) -> None:
        from PySide6.QtCore import QSettings

        settings = QSettings("UCM-Deployer", "paths")
        start = str(settings.value("last_file_dir", ""))
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", start, flt)
        if path:
            edit.setText(path)
            settings.setValue("last_file_dir", os.path.dirname(path))

    # ------------------------------------------------------------ 后台快任务
    def _run_quick(self, fn, on_done) -> None:
        """后台快任务：保持引用防止运行中线程被回收，完成后自动清理。"""
        task = QuickTask(fn, self)
        task.signals.sig_done.connect(on_done)

        def _cleanup(*_):
            if task in self._quick_tasks:
                self._quick_tasks.remove(task)

        task.signals.sig_done.connect(_cleanup)
        self._quick_tasks.append(task)
        task.start()

    # ------------------------------------------------------------ 镜像操作
    def _scan_images(self) -> None:
        """扫描服务器镜像并检查疑似 UCM 镜像，状态行给出「跳过/构建」结论。"""
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1点选要登录的服务器")
            return
        self._set_scan_state("镜像 UCM：正在扫描服务器镜像 ...", "#64748b")

        def fn(ssh, tctx):
            tctx.progress(30, "获取镜像列表")
            dm = DockerManager(ssh)
            if not dm.check_docker():
                raise RuntimeError("docker 不可用")
            images = dm.list_images()
            self._images = images
            tctx.log(f"共 {len(images)} 个镜像：")
            for im in images:
                mark = " [疑似UCM]" if "ucm" in im.ref.lower() else ""
                tctx.log(f"  {im.ref}    {im.size}{mark}")
            suspects = [im.ref for im in images if "ucm" in im.ref.lower()]
            installed = []
            if suspects:
                tctx.progress(60, f"检查 {len(suspects)} 个疑似 UCM 镜像")
                for ref in suspects:
                    ucm = dm.image_ucm_info(ref)
                    self._ucm_cache[ref] = ucm  # 底座选中时直接复用，不再 SSH
                    tctx.log(f"UCM 检查 {ref}: {ucm}")
                    if ucm.installed:
                        installed.append((ref, ucm))
                        break   # 找到一个即可下结论，停止检查剩余疑似镜像
            self._scan_installed = installed
            # 扫描正常结束（含发现 UCM 镜像提前停止）即上报 100%，
            # 避免进度条停留在最后一次过程进度（60%）
            tctx.progress(100, "扫描完成")

        def on_finished(all_ok: bool) -> None:
            if not all_ok:
                # 扫描失败（如新服务器未装 docker）：清空镜像列表，
                # 绝不显示上一台服务器的镜像，避免误导
                self._images = []
                self._scan_installed = []
                fill_image_combo(self.image_combo, [])  # 加载失败占位提示
                self._set_scan_state(
                    "✗ 扫描失败：无法获取服务器镜像（详见任务记录）——"
                    "如需使用 UCM，仍可在下方构建", "#d97706")
                return
            self._loaded_sid = server.id
            self._apply_images()
            installed = self._scan_installed or []
            if installed:
                ref = installed[0][0]
                self.ctx.images[server.id] = ref
                self._set_scan_state(
                    f"✓ 扫描完成：发现已有 UCM 镜像 {ref}——"
                    "本步骤可跳过，直接进入步骤3创建容器；也可在下方按需构建新镜像",
                    "#16a34a")
            else:
                self._set_scan_state(
                    "✗ 扫描完成：找不到 UCM 镜像——"
                    "如需使用 UCM，请在下方选择基础镜像并构建", "#d97706")

        self.panel.run_tasks([(server, fn)], "扫描镜像并检查 UCM",
                             on_finished=on_finished)

    def _apply_images(self) -> None:
        server = self.ctx.server
        current = self.ctx.images.get(server.id, "") if server else ""
        fill_image_combo(self.image_combo, self._images, current=current)
        self._on_base_image_changed(combo_ref(self.image_combo))
        self._suggest_tag_if_empty()

    def _upload_tar(self) -> None:
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return
        path = self.tar_path_edit.text().strip()
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "参数错误", "请先选择有效的镜像 tar/tar.gz 文件")
            return
        remote_dir = self.tar_remote_edit.text().strip() or "/tmp"
        if not remote_dir.startswith("/"):
            QMessageBox.warning(
                self, "参数错误",
                "服务器保存路径必须是绝对路径（以 / 开头），如 /tmp 或 /data")
            return

        def fn(ssh, tctx):
            dm = DockerManager(ssh)
            refs = upload_image_tar(
                ssh, dm, path, remote_dir=remote_dir,
                progress=lambda p, m: tctx.progress(p, m),
                log=tctx.log)
            if not refs:
                raise RuntimeError("docker load 未解析出镜像名")
            tctx.log("已加载: " + ", ".join(refs))
            self.ctx.images[server.id] = refs[0]
            for r in refs:
                if not any(im.ref == r for im in self._images):
                    self._images.append(image_from_ref(r))

        def on_finished(all_ok: bool) -> None:
            if all_ok:
                self._apply_images()
                self._select_source(0)
                # 先弹窗确认再重扫：模态框期间不启动新任务，
                # 避免扫描完成事件在弹窗内被处理（曾连环弹窗）
                QMessageBox.information(
                    self, "完成", "镜像已导入服务器，并已切换为“服务器已有镜像”。")
                self._scan_images()

        self.panel.run_tasks([(server, fn)], "上传镜像", on_finished=on_finished)

    def _pull_registry_image(self) -> None:
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return
        try:
            ref = validate_image_ref(self.registry_ref_edit.text())
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        self.registry_ref_edit.setText(ref)
        device = self.ctx.device_of(server)
        if not self._pull_command_dirty:
            self._update_pull_command()
        command = self.pull_command_edit.toPlainText().strip()
        try:
            command_tokens = shlex.split(command)
        except ValueError:
            command_tokens = []
        if (len(command_tokens) < 2
                or command_tokens[:2] != ["docker", "pull"]):
            QMessageBox.warning(
                self, "参数错误", "服务器拉取命令不能为空，且必须以 docker pull 开头")
            return
        command = " ".join(shq(token) for token in command_tokens)

        def fn(ssh, tctx):
            arch = device.arch or fetch_server_arch(ssh)
            platform = platform_from_arch(arch)
            manager = DockerManager(ssh)
            if not manager.check_docker():
                raise RuntimeError("目标服务器 Docker 不可用")
            tctx.log("$ " + command)
            tctx.progress(
                5, f"在服务器上拉取 {ref}"
                + (f"（{platform}）" if platform else "（服务器本机架构）"))
            result = ssh.exec_stream(command, on_line=tctx.log, timeout=7200)
            if not result.ok:
                raise RuntimeError(
                    f"服务器 docker pull 失败(退出码 {result.exit_code}): "
                    f"{(result.stderr or result.stdout).strip()[:400]}")
            if not manager.image_exists(ref):
                raise RuntimeError(f"docker pull 成功但未找到镜像 {ref}")
            tctx.progress(90, "确认服务器镜像列表")
            self.ctx.images[server.id] = ref
            if not any(im.ref == ref for im in self._images):
                self._images.append(image_from_ref(ref))
            tctx.progress(100, f"镜像已拉取到服务器: {ref}")

        def on_finished(all_ok: bool) -> None:
            if not all_ok:
                return
            self._apply_images()
            self._select_source(0)
            self._pull_command_dirty = False
            # 先弹窗确认再重扫（与导入/构建一致，避免模态框期间事件重入）
            QMessageBox.information(
                self, "完成",
                f"镜像 {ref} 已在目标服务器上拉取完成。\n"
                "已切换为“服务器已有镜像”，可将其选为构建底座。")
            self._scan_images()

        self.panel.run_tasks(
            [(server, fn)], f"服务器拉取镜像（{server.name}）",
            on_finished=on_finished)

    # ------------------------------------------------------------ 校验/构建
    def _local_args(self):
        """收集并本地校验输入；返回 (server, base, ucm, wrapt, toolkit) 或 None。"""
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return None
        base = self._selected_base()
        if base is None:
            return None
        ucm_whl = self.ucm_whl_edit.text().strip()
        if not ucm_whl or not os.path.isfile(ucm_whl):
            QMessageBox.warning(self, "参数错误", "请选择有效的 UCM whl 文件")
            return None
        wrapt_whl = self.wrapt_whl_edit.text().strip()
        if not wrapt_whl or not os.path.isfile(wrapt_whl):
            QMessageBox.warning(self, "参数错误", "请选择有效的 wrapt whl 文件")
            return None
        toolkit = self.toolkit_edit.text().strip()
        if toolkit and not os.path.isfile(toolkit):
            QMessageBox.warning(self, "参数错误", "ucm-toolkit whl 文件路径无效")
            return None
        return server, base, ucm_whl, wrapt_whl, toolkit

    def _check_match(self) -> None:
        args = self._local_args()
        if args is None:
            return
        server, base, ucm_whl, wrapt_whl, toolkit = args
        dev = self.ctx.device_of(server)
        self.match_label.setText(
            "匹配校验：正在检测基础镜像运行时并比对安装包...")

        def fn():
            from ...core.ssh_client import SSHClient
            with SSHClient(server) as ssh:
                runtime = fetch_image_runtime_info(ssh, base)
                arch = runtime.arch or fetch_server_arch(ssh)
                return validate_build_match(
                    dev, arch, base, ucm_whl, wrapt_whl,
                    image_runtime=runtime, toolkit_whl=toolkit)

        def on_done(result, error):
            if error is not None:
                self.match_label.setText(f"匹配校验：校验失败（{error}）")
                return
            if result.ok:
                self.match_label.setText(f"✓ 匹配校验：{result.message}"
                                         + ("".join(f"\n⚠ {w}" for w in result.warnings)))
            else:
                self.match_label.setText("✗ 匹配校验：" + result.message)
                QMessageBox.warning(self, "校验不通过", result.message)

        self._run_quick(fn, on_done)

    def _build(self) -> None:
        args = self._local_args()
        if args is None:
            return
        server, base, ucm_whl, wrapt_whl, toolkit = args
        tag = (self.tag_edit.text().strip()
               or suggest_tag(base, whl_version(ucm_whl)))
        dev = self.ctx.device_of(server)
        platform = "cuda" if dev.device_type == DeviceType.NVIDIA else "ascend"

        def fn(ssh, tctx):
            tctx.progress(3, "构建前校验匹配（基础镜像运行时/安装包）")
            runtime = fetch_image_runtime_info(ssh, base)
            arch = runtime.arch or fetch_server_arch(ssh)
            if runtime.summary:
                tctx.log("基础镜像运行时: " + runtime.summary)
            match = validate_build_match(
                dev, arch, base, ucm_whl, wrapt_whl,
                image_runtime=runtime, toolkit_whl=toolkit)
            for w in match.warnings:
                tctx.log("[警告] " + w)
            if not match.ok:
                raise RuntimeError("匹配校验不通过: " + match.message)

            cfg = ImageBuildConfig(
                base_image=base, image_tag=tag,
                ucm_whl_local=ucm_whl, wrapt_whl_local=wrapt_whl,
                toolkit_whl_local=toolkit,
                platform=platform)

            result = ImageBuilder(ssh, DockerManager(ssh)).build(
                cfg, progress=tctx.progress, log=tctx.log,
                cancelled=tctx.cancelled)
            tctx.log(f"构建完成: {result.image}"
                     f" (UCM {result.ucm.version if result.ucm else '?'})")
            self.ctx.images[server.id] = result.image
            if not any(im.ref == result.image for im in self._images):
                self._images.append(image_from_ref(result.image))

        def on_finished(all_ok: bool) -> None:
            if not all_ok:
                return
            self._apply_images()
            # 先弹窗确认再重扫：模态框期间不启动新任务，
            # 避免扫描完成事件在弹窗内被处理（曾连环弹窗）
            QMessageBox.information(
                self, "构建完成",
                f"镜像构建完成: {tag}\n已记录为本服务器使用的 UCM 镜像，"
                "可进入步骤3创建容器。")
            self._scan_images()

        self.panel.run_tasks([(server, fn)], f"构建镜像（{server.name}）",
                             on_finished=on_finished)
