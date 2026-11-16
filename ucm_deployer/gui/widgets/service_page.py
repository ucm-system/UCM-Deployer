# -*- coding: utf-8 -*-
"""步骤5：服务部署 —— 写入 UCM 配置文件并在容器内一键拉起推理服务。

两个输入框通过「容器内路径」衔接：
- 配置文件内容 → 部署时写入容器内路径（默认 /etc/ucm/config.yaml）；
- 拉起命令 → 由用户直接引用该路径，服务启动时读取配置。

页面参考批量部署的 TabWidget 模式：「UCM 配置文件」「推理服务拉起命令」
两个页签，各自带默认值与说明。部署按钮按流水线执行：检查容器 →
写配置 → 校验 → 后台拉起 → 回读启动日志；过程输出显示在底部日志区
（支持导出）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.docker_manager import DockerManager
from ...core.service_deploy import (
    DEFAULT_CONFIG_PATH,
    SERVICE_LOG_PATH,
    ServiceDeployConfig,
    deploy_service,
)
from ...core.ssh_client import SSHClient
from ..state import AppContext
from .common import ParallelTaskPanel, QuickTask, attach_resize_grip

_MONO_FONT = "Consolas"

# UCM 参数配置说明（华为支持文档，界面中可点击跳转）
UCM_DOC_URL = ("https://support.huawei.com/enterprise/zh/doc/EDOC1100582752/"
               "b4b5937b?idPath=7919749%7C251366268%7C250389224%7C"
               "263153904%7C268354339")

# UCM 配置文件默认模板（参数请按实际存储环境修改）
DEFAULT_UCM_CONFIG = (
    "# UCM 配置文件（默认模板）——以下参数请结合实际存储环境与业务按需修改，\n"
    "# 详细参数说明参考华为官方文档（界面下方链接可点击跳转）\n"
    "ucm_connectors:\n"
    "  - ucm_connector_name: \"UcmPipelineStore\"\n"
    "    ucm_connector_config:\n"
    "      store_pipeline: \"Cache|Posix\"\n"
    "      storage_backends: \"/mnt/test\"\n"
    "      posix_capacity_gb: 10240\n"
    "      posix_io_engine: \"psync\"\n"
    "      posix_data_trans_concurrency: 128\n"
)


class ServicePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._quick_tasks = []         # 运行中的 QuickTask 引用（防线程被回收）

        font = QFont(_MONO_FONT)
        font.setStyleHint(QFont.Monospace)

        # ---------- 1. 部署参数（目标容器）
        cfg_box = QGroupBox("1. 部署参数")
        self.container_combo = QComboBox()
        self.container_combo.setEditable(True)
        self.container_combo.setMinimumWidth(360)
        self.container_combo.setPlaceholderText("选择或输入要部署的容器名")
        self.container_combo.currentTextChanged.connect(self._on_container_changed)
        refresh_container_btn = QPushButton("🔄 刷新容器")
        refresh_container_btn.clicked.connect(self._refresh_containers)
        refresh_container_btn.setToolTip("读取服务器 docker ps -a，可选择已有容器")
        self.container_label = QLabel("目标容器：未选择")
        self.container_label.setProperty("role", "hint")

        lay1 = QVBoxLayout(cfg_box)
        lay1.setSpacing(4)
        container_row = QHBoxLayout()
        container_row.addWidget(QLabel("目标容器"), 0)
        container_row.addWidget(self.container_combo, 1)
        container_row.addWidget(refresh_container_btn)
        lay1.addLayout(container_row)
        lay1.addWidget(self.container_label)

        # ---------- 2. 部署配置（双页签：UCM 配置文件 / 拉起命令）
        tpl_box = QGroupBox("2. 部署配置")
        self.path_edit = QLineEdit(DEFAULT_CONFIG_PATH)
        self.path_edit.setToolTip(
            "配置文件将写入容器内此路径（目录不存在会自动创建）；\n"
            "拉起命令通过该路径读取 UCM 配置，两处路径必须一致。")

        self.content_edit = QPlainTextEdit()
        self.content_edit.setMinimumHeight(110)
        self.content_edit.setPlainText(DEFAULT_UCM_CONFIG)
        attach_resize_grip(self.content_edit)
        self.content_edit.setFont(font)

        self.start_edit = QPlainTextEdit()
        self.start_edit.setMinimumHeight(110)
        self.start_edit.setPlaceholderText(
            "拉起命令默认为空，请按实际填写，如：\n"
            "vllm serve /models/Qwen --ucm-config /etc/ucm/config.yaml ...")
        attach_resize_grip(self.start_edit)
        self.start_edit.setFont(font)
        self.start_edit.setToolTip(
            "在目标容器内以后台方式（nohup）执行；服务输出写入容器内 "
            + SERVICE_LOG_PATH + "，部署完成后自动回读显示。\n"
            "请在命令中直接引用「配置文件路径」，服务启动时从该路径读取配置。")

        self.tabs = QTabWidget()
        # Tab1：UCM 配置文件
        cfg_page = QWidget()
        lay_cfg = QVBoxLayout(cfg_page)
        lay_cfg.setContentsMargins(8, 8, 8, 8)
        lay_cfg.setSpacing(4)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("配置文件路径（容器内）"), 0)
        path_row.addWidget(self.path_edit, 1)
        content_hint = QLabel(
            "以上为默认 UCM 配置模板（存储路径/容量/并发等请按实际环境修改）；"
            f"详细参数说明见 <a href=\"{UCM_DOC_URL}\">UCM 参数配置说明（华为官方文档）</a>")
        content_hint.setTextFormat(Qt.RichText)
        content_hint.setOpenExternalLinks(True)
        content_hint.setProperty("role", "hint")
        lay_cfg.addLayout(path_row)
        lay_cfg.addWidget(self.content_edit, 1)
        lay_cfg.addWidget(content_hint)
        # Tab2：推理服务拉起命令
        cmd_page = QWidget()
        lay_cmd = QVBoxLayout(cmd_page)
        lay_cmd.setContentsMargins(8, 8, 8, 8)
        lay_cmd.setSpacing(4)
        cmd_hint = QLabel(
            "拉起命令默认为空，请按实际模型路径与推理引擎填写；"
            f"命令在容器内后台执行（输出写入容器内 {SERVICE_LOG_PATH}），"
            "请在命令中引用上方配置文件路径")
        cmd_hint.setTextFormat(Qt.RichText)
        cmd_hint.setOpenExternalLinks(True)
        cmd_hint.setProperty("role", "hint")
        lay_cmd.addWidget(self.start_edit, 1)
        lay_cmd.addWidget(cmd_hint)
        self.tabs.addTab(cfg_page, "UCM 配置文件")
        self.tabs.addTab(cmd_page, "推理服务拉起命令")

        lay2 = QVBoxLayout(tpl_box)
        lay2.setSpacing(6)
        lay2.addWidget(self.tabs, 1)

        # ---------- 3. 操作行
        deploy_btn = QPushButton("🚀 部署UCM推理服务")
        deploy_btn.setProperty("accent", True)
        deploy_btn.setToolTip(
            "一键执行：检查容器 → 写入配置文件 → 校验 → 后台拉起服务 →\n"
            "回读服务启动日志；全过程输出见下方日志区。")
        deploy_btn.clicked.connect(self._run)
        self.deploy_btn = deploy_btn
        self.status_label = QLabel("未部署")
        self.status_label.setProperty("role", "hint")
        run_row = QHBoxLayout()
        run_row.addWidget(deploy_btn)
        run_row.addWidget(self.status_label)
        run_row.addStretch(1)

        # ---------- 4. 部署日志（常驻；部署前显示占位，部署后填充过程日志）
        self.panel = ParallelTaskPanel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)              # 与步骤3/4 一致：模块间顺序感
        layout.addWidget(cfg_box)
        layout.addWidget(tpl_box, 1)
        layout.addLayout(run_row)
        layout.addWidget(self.panel, 1)    # 富余高度全部给日志区

    # ------------------------------------------------------------ 辅助
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

    def on_enter(self) -> None:
        server = self.ctx.server
        current = self.ctx.containers.get(server.id, "") if server else ""
        if current and not self._selected_container():
            self.container_combo.blockSignals(True)
            self.container_combo.setCurrentText(current)
            self.container_combo.blockSignals(False)
        self._refresh_container_label()
        self._refresh_containers()

    def _on_container_changed(self, text: str) -> None:
        container = text.strip()
        server = self.ctx.server
        if server is not None and container:
            self.ctx.containers[server.id] = container
        self._refresh_container_label()

    def _selected_container(self) -> str:
        return self.container_combo.currentText().strip()

    def _refresh_container_label(self) -> None:
        server = self.ctx.server
        container = self._selected_container()
        if server is None:
            self.container_label.setText("目标容器：未登录服务器（请先在步骤1 点选）")
        elif not container:
            self.container_label.setText("目标容器：未选择")
        else:
            self.container_label.setText(f"目标容器：{container}")

    def _refresh_containers(self) -> None:
        server = self.ctx.server
        if server is None:
            self.container_combo.blockSignals(True)
            self.container_combo.clear()
            self.container_combo.blockSignals(False)
            self._refresh_container_label()
            return

        def fn():
            with SSHClient(server) as ssh:
                return DockerManager(ssh).list_containers(all_states=True)

        def on_done(containers, error):
            names = [c.get("names", "") for c in (containers or [])
                     if c.get("names")]
            current = self.ctx.containers.get(server.id, "")
            self.container_combo.blockSignals(True)
            self.container_combo.clear()
            if current and current not in names:
                names.insert(0, current)
            self.container_combo.addItems(names)
            if current:
                self.container_combo.setCurrentText(current)
            self.container_combo.blockSignals(False)
            self._refresh_container_label()

        self._run_quick(fn, on_done)

    # ------------------------------------------------------------ 执行
    def _run(self) -> None:
        server = self.ctx.server
        if server is None:
            QMessageBox.information(self, "提示", "请先在步骤1勾选要登录的服务器")
            return
        container = self._selected_container()
        if not container:
            QMessageBox.information(
                self, "提示", "请先选择目标容器（步骤3 创建的容器）")
            return
        config_path = self.path_edit.text().strip()
        config_content = self.content_edit.toPlainText()
        start_cmd = self.start_edit.toPlainText().strip()
        try:
            cfg = ServiceDeployConfig(
                container=container, config_path=config_path,
                config_content=config_content, start_cmd=start_cmd)
            cfg.validate()
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return

        self.status_label.setText("部署中...（过程与启动日志见下方页签）")
        holder = {}

        def fn(ssh, tctx):
            tctx.progress(10, f"开始部署：容器 {container}")
            report = deploy_service(
                ssh, cfg,
                progress=lambda p, m: tctx.progress(p, m),
                log=lambda line: tctx.log(line))
            holder["report"] = report

        def on_finished(all_ok: bool) -> None:
            report = holder.get("report")
            if report is not None and report.ok:
                self.status_label.setText(
                    "部署完成：配置已写入，服务已后台拉起（日志见下方页签）")
            elif not all_ok:
                self.status_label.setText("部署失败，详见日志页签")

        self.panel.run_tasks([(server, fn)], f"服务部署（容器 {container}）",
                             on_finished=on_finished)
