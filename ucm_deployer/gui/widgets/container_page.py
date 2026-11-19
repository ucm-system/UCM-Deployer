# -*- coding: utf-8 -*-
"""步骤3：容器创建 —— 选择 UCM 镜像（选择时自动校验）、docker run 生成/编辑/执行。

排版目标：1280x860 一屏可见（无滚动条）——挂载配置合并为一个分组，
提示文字精简，详细说明放 tooltip。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
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
    QVBoxLayout,
    QWidget,
)

from ...core.container_manager import (ContainerCreateConfig,
                                       ContainerManager, check_shared_fs,
                                       evaluate_shared_fs)
from ...core.docker_manager import DockerError, DockerManager
from ...core.models import DeviceType, UCMInfo, VolumeMount
from ...core.ssh_client import SSHClient
from ..state import AppContext
from .common import (ParallelTaskPanel, QuickTask, RemoteDirDialog, combo_ref,
                     attach_resize_grip, fill_image_combo, setup_image_combo)


class _PathPickRow(QWidget):
    """路径输入 + 浏览（远端目录）"""

    def __init__(self, page: "ContainerPage", placeholder: str = "/mnt/..."):
        super().__init__()
        self.page = page
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        browse = QPushButton("浏览...")
        browse.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)

    def _browse(self) -> None:
        server = self.page.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return

        def lister(path: str) -> List[str]:
            with SSHClient(server) as ssh:
                return ContainerManager(ssh).list_host_dirs(path)

        start = self.edit.text().strip() or "/"
        dlg = RemoteDirDialog(self, lister, start=start)
        if dlg.exec() == QDialog.Accepted and dlg.selected_path():
            self.edit.setText(dlg.selected_path())

    def text(self) -> str:
        return self.edit.text().strip()


class ContainerPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._images = []
        self._loaded_sid: Optional[str] = None      # 已加载镜像列表的服务器
        self._ucm_state: Dict[str, UCMInfo] = {}   # 镜像 ref -> UCM 校验结果
        self._quick_tasks = []                      # 运行中的 QuickTask 引用
        self._create_failed: Dict[str, str] = {}    # server_id -> 创建失败的容器名
        self._ascend_devices: Optional[List[str]] = None
        self._ascend_mounts: Optional[List[tuple]] = None
        self._ascend_devices_loading = False

        # ---------- 1. UCM 镜像选择
        top_box = QGroupBox("1. UCM 镜像选择")
        self.image_combo = QComboBox()
        setup_image_combo(self.image_combo)
        self.image_combo.setMinimumWidth(420)
        self.image_combo.currentIndexChanged.connect(self._on_image_changed)
        self.image_combo.setToolTip(
            "选择用于创建容器的 UCM 镜像：步骤2 构建的镜像，或服务器上已含 UCM 的镜像。\n"
            "未列出请点「刷新」；选中后自动校验镜像内 UCM 安装情况。")
        refresh_img_btn = QPushButton("🔄 刷新")
        refresh_img_btn.clicked.connect(self._refresh_images)
        self.ucm_check_label = QLabel("镜像 UCM：未校验")
        self.ucm_check_label.setProperty("role", "hint")
        self.ucm_check_label.setWordWrap(True)
        self.ucm_check_label.setToolTip("选择镜像后自动在服务器上校验镜像内 UCM 安装情况")
        self.reuse_hint = QLabel(
            "已有安装 UCM 的容器时，可跳过本页创建操作，直接进入步骤4选择该容器预检。")
        self.reuse_hint.setProperty("role", "hint")
        self.reuse_hint.setWordWrap(True)
        self.ascend_device_label = QLabel("Ascend 设备节点：将按服务器实际存在情况自动映射")
        self.ascend_device_label.setProperty("role", "hint")
        self.ascend_device_label.setWordWrap(True)
        self.ascend_device_label.setVisible(False)   # 仅 Ascend 服务器显示

        self.name_edit = QLineEdit("ucm-vllm")
        self.name_edit.setFixedWidth(180)
        self.name_edit.setToolTip("docker 容器名（--name）；步骤4 按此名找到容器进行预检")
        self.shm_edit = QLineEdit("512g")
        self.shm_edit.setFixedWidth(90)
        self.shm_edit.setToolTip(
            "容器共享内存大小（--shm-size）；vLLM 张量并行需要大共享内存，默认 512g")
        self.net_edit = QLineEdit("host")
        self.net_edit.setFixedWidth(110)
        self.net_edit.setToolTip(
            "host = 容器共享宿主机网络栈（Ascend 推理推荐）；\n"
            "NVIDIA 容器自动改用端口映射，此项忽略")

        img_row = QHBoxLayout()
        img_row.addWidget(self.image_combo, 1)
        img_row.addWidget(refresh_img_btn)
        img_form = QFormLayout()
        img_form.addRow("UCM 镜像", img_row)
        top_layout = QVBoxLayout(top_box)
        top_layout.setSpacing(4)
        top_layout.addLayout(img_form)
        top_layout.addWidget(self.ucm_check_label)
        top_layout.addWidget(self.reuse_hint)
        top_layout.addWidget(self.ascend_device_label)

        # ---------- 2. 挂载（统一表：KV Cache / 模型 / 自定义，一套按钮）
        mount_box = QGroupBox("2. 挂载（KV Cache / 模型 / 自定义）")

        self.mount_type = QComboBox()
        self.mount_type.addItems(["KV Cache", "模型", "自定义"])
        self.mount_type.setToolTip(
            "KV Cache：挂载到容器内相同路径（多目录须位于同一文件系统）\n"
            "模型：模型文件目录（默认只读，表中最多一行）\n"
            "自定义：其他任意目录映射")
        self.mount_host = _PathPickRow(self, "/mnt/kvcache 或 /data/models/...")
        self.mount_host.edit.textChanged.connect(self._sync_mount_container)
        self.mount_container = QLineEdit()
        self.mount_container.setPlaceholderText("容器内路径")
        mount_add_btn = QPushButton("➕ 添加")
        mount_add_btn.clicked.connect(self._mount_add)

        self.mount_table = QTableWidget(0, 4)
        self.mount_table.setHorizontalHeaderLabels(
            ["类型", "宿主机路径", "容器内路径", "只读"])
        self.mount_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.mount_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.mount_table.verticalHeader().setVisible(False)
        self.mount_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.mount_table.setMaximumHeight(120)
        self.mount_table.setToolTip(
            "所有挂载类型共用本表，操作统一：上方输入后「➕ 添加」，选中行「🗑 移除」。\n"
            "KV Cache 行容器内路径自动与宿主机相同（vLLM 按相同路径读取）。\n"
            "默认表为空（即不挂载任何目录）；需要挂载模型时选择类型后添加。")

        mount_del_btn = QPushButton("🗑 移除选中")
        mount_check_btn = QPushButton("🔍 校验选中挂载")
        mount_check_btn.setToolTip(
            "按表格中选中的行校验（按住 Ctrl 可多选）：\n"
            "· 通用：目录是否存在、位于什么文件系统（本地盘 / NFS / 3FS 等）\n"
            "· 选中的多个 KV Cache 行：还要求位于同一文件系统"
            "（vLLM 多目录 KV cache 的要求）\n"
            "· 模型/自定义行：仅报告文件系统信息，无硬性要求")
        mount_del_btn.clicked.connect(self._mount_del)
        mount_check_btn.clicked.connect(self._kv_check)

        mount_layout = QVBoxLayout(mount_box)
        mount_layout.setSpacing(4)
        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("类型"))
        add_row.addWidget(self.mount_type)
        add_row.addWidget(self.mount_host, 1)      # 与表格列宽一致：宿主机 : 容器内 = 1 : 1
        add_row.addWidget(QLabel("→ 容器内"))
        add_row.addWidget(self.mount_container, 1)
        add_row.addWidget(mount_add_btn)
        mount_layout.addLayout(add_row)
        mount_layout.addWidget(self.mount_table)
        mount_btns = QHBoxLayout()
        mount_btns.addWidget(mount_del_btn)
        mount_btns.addWidget(mount_check_btn)
        mount_btns.addStretch(1)
        mount_layout.addLayout(mount_btns)

        # ---------- 3. 容器配置与创建
        cmd_box = QGroupBox("3. 容器配置与创建")
        regen_btn = QPushButton("🔁 生成/刷新命令")
        create_btn = QPushButton("🚀 创建容器")
        create_btn.setProperty("accent", True)
        check_ctr_btn = QPushButton("✅ 检查容器内 UCM")
        check_ctr_btn.setToolTip(
            "检查上方「容器名」输入框所指定容器的 UCM 安装情况；\n"
            "检查对象与详细结果见下方任务记录的标题与日志。")
        regen_btn.clicked.connect(self._regen)
        create_btn.clicked.connect(self._create)
        check_ctr_btn.clicked.connect(self._check_container_ucm)
        self.cmd_edit = QPlainTextEdit()
        self.cmd_edit.setProperty("role", "code")
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.cmd_edit.setFont(font)
        self.cmd_edit.setPlaceholderText("点击「生成/刷新命令」生成 docker run 命令")
        self.cmd_edit.setMinimumHeight(110)
        attach_resize_grip(self.cmd_edit)   # 右下角拖动调整高度
        self.cmd_edit.setToolTip(
            "自动识别服务器卡数并映射全部加速卡：\n"
            "NPU: 全部 /dev/davinci* 设备 + 驱动挂载 + --net=host\n"
            "GPU: --runtime nvidia --gpus all -p 8000:8000\n"
            "固定 --entrypoint /bin/bash：只创建并保持容器运行，不启动 vLLM\n"
            "命令可编辑，创建容器时按编辑框内容执行。")

        cmd_layout = QVBoxLayout(cmd_box)
        cmd_layout.setSpacing(4)
        # 参数输入框在左（固定宽度）、操作按钮在右，合并为一行（节省纵向空间）
        params_row = QHBoxLayout()
        params_row.addWidget(QLabel("容器名"))
        params_row.addWidget(self.name_edit)
        params_row.addWidget(QLabel("共享内存"))
        params_row.addWidget(self.shm_edit)
        params_row.addWidget(QLabel("网络"))
        params_row.addWidget(self.net_edit)
        params_row.addStretch(1)
        params_row.addWidget(regen_btn)
        params_row.addWidget(check_ctr_btn)
        params_row.addWidget(create_btn)
        cmd_layout.addLayout(params_row)
        cmd_layout.addWidget(self.cmd_edit, 1)

        self.panel = ParallelTaskPanel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)              # 模块间留白，强化 1→2→3 顺序感
        layout.addWidget(top_box)
        layout.addWidget(mount_box)
        layout.addWidget(cmd_box, 1)
        layout.addWidget(self.panel, 2)

    # ------------------------------------------------------------ 辅助
    def on_enter(self) -> None:
        server = self.ctx.server
        if server is None:
            self._reset_for_server_switch()
            self.ascend_device_label.setVisible(False)
            fill_image_combo(self.image_combo, [])
            self.ucm_check_label.setText("镜像 UCM：未登录服务器（请回步骤1勾选）")
            return
        if self._loaded_sid != server.id:
            self._reset_for_server_switch()
        self._refresh_ascend_devices()
        self._refresh_images()

    def _reset_for_server_switch(self) -> None:
        """切换服务器：清理上一台的镜像/UCM 校验/设备探测与命令内容。

        不清理的话：下拉框残留上一台的镜像、命令框残留上一台的 docker run
        （设备节点/容器名都是上一台的），点「创建容器」会按旧命令执行。
        """
        self._loaded_sid = None
        self._images = []
        self._ucm_state = {}
        self._ascend_devices = None
        self._ascend_mounts = None
        self.ascend_device_label.setVisible(False)
        fill_image_combo(self.image_combo, [])
        self.ucm_check_label.setText("镜像 UCM：未校验")
        self.cmd_edit.clear()

    def current_image(self) -> str:
        return combo_ref(self.image_combo)

    def _refresh_ascend_devices(self, regenerate_when_done: bool = False) -> None:
        server = self.ctx.server
        device = self.ctx.device_of(server) if server else None
        if server is None or device.device_type != DeviceType.ASCEND:
            self._ascend_devices = []
            self._ascend_mounts = []
            self._ascend_devices_loading = False
            self.ascend_device_label.setVisible(False)   # 非 Ascend 服务器不显示
            if regenerate_when_done:
                self._regen()
            return
        if self._ascend_devices_loading:
            return

        self._ascend_devices_loading = True
        self._ascend_devices = None
        self._ascend_mounts = None
        self.ascend_device_label.setText("Ascend 设备节点：正在探测实际 /dev 节点...")

        def fn():
            with SSHClient(server) as ssh:
                manager = ContainerManager(ssh)
                return manager.list_ascend_devices(), manager.list_ascend_mounts()

        def on_done(resources, error):
            self._ascend_devices_loading = False
            if error is not None:
                self._ascend_devices = []
                self._ascend_mounts = []
                self.ascend_device_label.setText(
                    "Ascend 设备节点：探测失败，将使用兼容默认映射（可重试）")
            else:
                self._ascend_devices, self._ascend_mounts = resources
                self._ascend_devices = self._ascend_devices or []
                self._ascend_mounts = self._ascend_mounts or []
                davinci_count = sum(
                    node.startswith("/dev/davinci")
                    for node in self._ascend_devices
                )
                self.ascend_device_label.setText(
                    f"Ascend 设备节点：已探测 {davinci_count} 个 davinci 节点、"
                    f"{len(self._ascend_mounts)} 个系统挂载；仅映射实际存在的资源")
            if regenerate_when_done:
                self._regen()

        self._run_quick(fn, on_done)

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

    def _on_image_changed(self, *_) -> None:
        """选择镜像时自动校验镜像内 UCM 安装情况。"""
        ref = self.current_image()
        if not ref:
            self.ucm_check_label.setText("镜像 UCM：未校验")
            return
        if ref in self._ucm_state:
            self._show_ucm_state(ref)
            return
        server = self.ctx.server
        if server is None:
            return
        self.ucm_check_label.setText(f"镜像 UCM：正在检查 {ref} ...")

        def fn():
            with SSHClient(server) as ssh:
                return DockerManager(ssh).image_ucm_info(ref)

        def on_done(ucm, error):
            if error is not None:
                self._ucm_state[ref] = UCMInfo()
                self.ucm_check_label.setText(f"✗ 镜像 UCM：检查失败（{error}）")
                return
            self._ucm_state[ref] = ucm
            self._show_ucm_state(ref)

        self._run_quick(fn, on_done)

    def _show_ucm_state(self, ref: str) -> None:
        ucm = self._ucm_state.get(ref)
        server = self.ctx.server
        if ucm is None:
            return
        if ucm.installed:
            self.ucm_check_label.setText(f"✓ 镜像 UCM：已安装 {ucm.version}")
            if server is not None:
                self.ctx.images[server.id] = ref
        else:
            self.ucm_check_label.setText(
                f"✗ 镜像 UCM：{ref} 未安装 UCM（请回步骤2构建，或选择其他镜像）")

    # ------------------------------------------------------------ 命令生成
    def _build_config(self) -> ContainerCreateConfig:
        server = self.ctx.server
        kv_dirs: List[str] = []
        model_mount = None
        extra_mounts: List[VolumeMount] = []
        for row in range(self.mount_table.rowCount()):
            def cell(r: int, c: int) -> str:
                it = self.mount_table.item(r, c)
                return it.text().strip() if it is not None else ""
            mtype = cell(row, 0)
            host = cell(row, 1)
            cont = cell(row, 2)
            ro_item = self.mount_table.item(row, 3)
            ro = ro_item is not None and ro_item.checkState() == Qt.Checked
            if not host:
                continue
            if mtype == "KV Cache":
                kv_dirs.append(host)
            elif mtype == "模型":
                if model_mount is None:
                    model_mount = VolumeMount(host, cont or "/models", ro)
            else:
                extra_mounts.append(VolumeMount(host, cont or host, ro))
        dev = self.ctx.device_of(server) if server else None
        privileged = any(
            node in ("/dev/ummu", "/dev/uburma")
            for node in (self._ascend_devices or [])
        )
        return ContainerCreateConfig(
            image=self.current_image(), name=self.name_edit.text().strip(),
            device_type=dev.device_type if dev else DeviceType.UNKNOWN,
            device_count=dev.count if dev else 0,
            shm_size=self.shm_edit.text().strip() or "512g",
            network=self.net_edit.text().strip(),
            kv_cache_dirs=kv_dirs, model_mount=model_mount,
            extra_mounts=extra_mounts,
            device_nodes=self._ascend_devices or [],
            system_mounts=self._ascend_mounts or [],
            privileged=privileged)

    def _regen(self) -> None:
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return
        device = self.ctx.device_of(server)
        if (device.device_type == DeviceType.ASCEND
                and (self._ascend_devices is None or self._ascend_mounts is None)):
            self._refresh_ascend_devices(regenerate_when_done=True)
            return
        try:
            cmd = ContainerManager.generate_run_command(self._build_config())
        except Exception as exc:
            QMessageBox.warning(self, "生成失败", str(exc))
            return
        self.cmd_edit.setPlainText(cmd)

    def command_text(self) -> str:
        return self.cmd_edit.toPlainText()

    # ------------------------------------------------------------ 挂载
    def _add_mount_row(self, mtype: str, host: str, container: str,
                       ro: bool, lock_container: bool = False) -> None:
        """向统一挂载表添加一行；lock_container 用于 KV Cache 同路径锁定。"""
        table = self.mount_table
        row = table.rowCount()
        table.insertRow(row)
        t = QTableWidgetItem(mtype)
        t.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        table.setItem(row, 0, t)
        table.setItem(row, 1, QTableWidgetItem(host))
        c = QTableWidgetItem(container)
        if lock_container:
            c.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        table.setItem(row, 2, c)
        ro_item = QTableWidgetItem()
        ro_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        ro_item.setCheckState(Qt.Checked if ro else Qt.Unchecked)
        table.setItem(row, 3, ro_item)

    def _sync_mount_container(self, text: str) -> None:
        """KV Cache 类型：容器内路径自动跟随宿主机（同路径约定）。"""
        if self.mount_type.currentIndex() == 0:
            self.mount_container.setText(text.strip())

    def _mount_add(self) -> None:
        host = self.mount_host.text()
        if not host.startswith("/"):
            QMessageBox.warning(self, "参数错误", "宿主机路径必须是以 / 开头的绝对路径")
            return
        mtype = self.mount_type.currentText()
        if mtype == "模型":
            for r in range(self.mount_table.rowCount()):
                if self.mount_table.item(r, 0).text() == "模型":
                    QMessageBox.information(
                        self, "提示", "模型挂载仅一行，请直接编辑表中已有的「模型」行")
                    return
            self._add_mount_row("模型", host,
                                self.mount_container.text().strip() or "/models",
                                True)
        elif mtype == "KV Cache":
            self._add_mount_row("KV Cache", host, host, False, lock_container=True)
        else:
            self._add_mount_row("自定义", host,
                                self.mount_container.text().strip() or host, False)
        self.mount_host.edit.clear()
        self.mount_container.clear()

    def _mount_del(self) -> None:
        row = self.mount_table.currentRow()
        if row >= 0:
            self.mount_table.removeRow(row)

    def _kv_check(self) -> None:
        """校验选中的挂载行（可多选，见按钮 tooltip）：

        - 通用：目录存在性 + 所在文件系统（df 的 source/fstype）；
        - 多个 KV Cache 行：额外要求同一文件系统（vLLM 多目录 KV cache 要求）；
        - 模型/自定义行：仅报告文件系统信息，不做同源硬判断。
        """
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1点选要登录的服务器")
            return
        rows = sorted({idx.row() for idx in
                       self.mount_table.selectionModel().selectedRows()})
        if not rows:
            QMessageBox.information(
                self, "提示", "请先在表格中选中要校验的挂载行（按住 Ctrl 可多选）")
            return
        kv_dirs: List[str] = []
        other_dirs: List[str] = []
        for r in rows:
            t = self.mount_table.item(r, 0)
            h = self.mount_table.item(r, 1)
            if t is None or h is None or not h.text().strip():
                continue
            (kv_dirs if t.text() == "KV Cache" else other_dirs).append(
                h.text().strip())
        if not kv_dirs and not other_dirs:
            QMessageBox.information(self, "提示", "选中行没有有效的宿主机路径")
            return

        def fn():
            with SSHClient(server) as ssh:
                kv_report = check_shared_fs(ssh, kv_dirs) if kv_dirs else None
                other_report = (check_shared_fs(ssh, other_dirs)
                                if other_dirs else None)
                return kv_report, other_report

        def on_done(result, error):
            if error is not None or result is None:
                QMessageBox.warning(self, "校验失败", str(error))
                return
            kv_report, other_report = result
            parts: List[str] = []
            ok = True
            if kv_report is not None:
                merged = evaluate_shared_fs(kv_report.entries)
                ok = ok and merged.ok
                parts.append("【KV Cache】" + merged.message)
                parts.extend(merged.warnings)
            if other_report is not None:
                errs = [e for e in other_report.entries if e.error]
                if errs:
                    ok = False
                    parts.append("【目录异常】" + "；".join(
                        f"{e.path} {e.error}" for e in errs))
                else:
                    parts.append("【模型/自定义】" + "；".join(
                        f"{e.path} 位于 {e.source}（{e.fstype or '未知类型'}）"
                        for e in other_report.entries))
            icon = QMessageBox.Information if ok else QMessageBox.Warning
            box = QMessageBox(icon, "挂载校验", "\n".join(parts))
            box.exec()

        self._run_quick(fn, on_done)

    # ------------------------------------------------------------ 镜像操作
    def _refresh_images(self) -> None:
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return

        def fn():
            with SSHClient(server) as ssh:
                return DockerManager(ssh).list_images()

        def on_done(images, error):
            if error is not None:
                # 获取失败（如新服务器未装 docker）：清空下拉框，
                # 绝不显示上一台服务器的镜像，避免误导
                self._images = []
                fill_image_combo(self.image_combo, [])
                self.ucm_check_label.setText("✗ 镜像列表获取失败（详见任务记录）")
                return
            self._images = images or []
            self._loaded_sid = server.id
            current = self.ctx.images.get(server.id, "")
            fill_image_combo(self.image_combo, self._images, current=current)
            # fill_image_combo 内部 blockSignals，手动触发选中镜像的 UCM 校验
            if self.current_image():
                self._on_image_changed()

        self._run_quick(fn, on_done)

    # ------------------------------------------------------------ 创建
    def _create(self) -> None:
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return
        ref = self.current_image()
        if not ref:
            QMessageBox.warning(self, "参数错误", "请先选择镜像（刷新镜像列表后选择）")
            return
        ucm = self._ucm_state.get(ref)
        if ucm is None:
            QMessageBox.warning(
                self, "未校验 UCM",
                f"镜像 {ref} 的 UCM 安装情况尚未校验完成，请稍候或重新选择镜像。")
            return
        if not ucm.installed:
            QMessageBox.warning(
                self, "镜像未安装 UCM",
                f"镜像 {ref} 未安装 UCM。\n请回步骤2构建带 UCM 的镜像，或选择其他镜像。")
            return

        text = self.command_text()
        if not text.strip():
            try:
                text = ContainerManager.generate_run_command(self._build_config())
                self.cmd_edit.setPlainText(text)
            except Exception as exc:
                QMessageBox.warning(self, "参数错误", str(exc))
                return

        cmd = text

        def fn(ssh, tctx):
            tctx.progress(20, "创建容器...")
            cm = ContainerManager(ssh, DockerManager(ssh))
            tctx.log("$ " + cmd.replace("\n", " \\\n"))
            try:
                cm.create(cmd, on_line=tctx.log)
            except DockerError:
                # 详细原因（如容器名冲突）已在日志中；状态栏保持简短提示
                raise DockerError("容器创建失败（详细原因见下方日志）") from None
            name = self.name_edit.text().strip()
            self.ctx.containers[server.id] = name
            tctx.progress(100, f"容器 {name} 已创建")

        def on_finished(all_ok: bool) -> None:
            if all_ok:
                self._create_failed[server.id] = ""
                QMessageBox.information(
                    self, "完成",
                    f"容器创建完成: {self.name_edit.text().strip()}\n"
                    "可执行「检查容器内 UCM」确认容器可用。")
            else:
                # 记录创建失败的容器名：此时服务器上可能存在同名旧容器，
                # 后续「检查容器内 UCM」针对的将是旧容器，需向用户澄清
                self._create_failed[server.id] = self.name_edit.text().strip()

        self.panel.run_tasks([(server, fn)], "创建容器", on_finished=on_finished)

    def _check_container_ucm(self) -> None:
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "参数错误", "请先填写容器名")
            return

        # 创建失败后，同名容器是服务器上已有的旧容器：先确认检查对象，
        # 避免「创建失败却检查通过」让用户误以为本次创建成功了
        if self._create_failed.get(server.id) == name:
            ret = QMessageBox.question(
                self, "确认检查对象",
                f"容器 {name} 本次创建未成功，服务器上存在同名旧容器。\n"
                "继续执行将检查该已有容器的 UCM 安装情况。\n\n是否继续？")
            if ret != QMessageBox.StandardButton.Yes:
                return

        def fn(ssh, tctx):
            ucm = DockerManager(ssh).container_ucm_info(name)
            tctx.log(f"容器 {name}: {ucm}")
            if self._create_failed.get(server.id) == name:
                tctx.log("[deployer] 注意：该容器为服务器上已存在的同名容器，"
                         "本次创建未成功，以上为该已有容器的检查结果")
            if not ucm.installed:
                raise RuntimeError(f"容器 {name} 内未检测到 UCM")
            self.ctx.containers[server.id] = name
            # 状态栏保持简短（过长会把页面拉长）；详细说明见日志
            return f"容器 {name} 已安装 UCM {ucm.version}"

        self.panel.run_tasks([(server, fn)], f"检查容器 UCM（{name}）")
