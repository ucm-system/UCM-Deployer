# -*- coding: utf-8 -*-
"""步骤5：服务部署 —— 纯操作指引页。

页面分三个模块（阅读顺序 = 操作顺序）：
1. UCM 配置文件：参考默认配置 + 参数说明官方网址（可点击），
   需要按环境修改的参数以高亮显示；
2. 推理服务拉起命令：容器内执行的命令模板，包含必需的环境变量导出
   （ENABLE_UCM_PATCH / VLLM_CPU_AFFINITY）与 --kv-transfer-config
   接入 UCMConnector 的配置块；需要修改的占位（模型路径、UCM_CONFIG_FILE）
   以高亮显示；
3. 验证连通性：按输入的端口与模型名自动生成 curl 验证命令
   （服务就绪后在宿主机另开终端执行）。

本页为静态指引（展示 + 可复制），不含任何执行按钮。
"""
from __future__ import annotations

import html as _html

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..state import AppContext
from .common import attach_resize_grip

_MONO_FONT = "Consolas"

# 高亮样式：需要用户按环境修改的片段（浅黄底 + 深棕字 + 加粗）
_HIGHLIGHT_STYLE = "background-color:#fef3c7; color:#b45309; font-weight:bold"

# UCM 参数配置说明（华为支持文档，界面中可点击跳转）
UCM_DOC_URL = ("https://support.huawei.com/enterprise/zh/doc/EDOC1100582752/"
               "b4b5937b?idPath=7919749%7C251366268%7C250389224%7C"
               "263153904%7C268354339")

# 模块1：UCM 配置文件参考默认模板（参数请按实际存储环境修改）
UCM_CONFIG_TEMPLATE = (
    "ucm_connectors:\n"
    "  - ucm_connector_name: \"UcmPipelineStore\"\n"
    "    ucm_connector_config:\n"
    "      store_pipeline: \"Cache|Posix\"\n"
    "      storage_backends: \"/mnt/test\"\n"
    "      posix_capacity_gb: 10240\n"
    "      posix_io_engine: \"psync\"\n"
    "      posix_data_trans_concurrency: 128\n"
)

# 模块2：推理服务拉起命令模板（容器内执行）
START_CMD_TEMPLATE = (
    "# 拉起服务前先导出环境变量（UCM 补丁开关 + CPU 亲和）\n"
    "export ENABLE_UCM_PATCH=1\n"
    "export VLLM_CPU_AFFINITY=1\n"
    "\n"
    "# 拉起推理服务（模型路径/端口等按实际填写），\n"
    "# 必须附加 --kv-transfer-config 以接入 UCM：\n"
    "vllm serve /models/Qwen ... \\\n"
    "    --kv-transfer-config '{\n"
    "        \"kv_connector\": \"UCMConnector\",\n"
    "        \"kv_connector_module_path\": \"ucm.integration.vllm.ucm_connector\",\n"
    "        \"kv_role\": \"kv_both\",\n"
    "        \"kv_connector_extra_config\": {\n"
    "            \"UCM_CONFIG_FILE\": \"/workspace/ucm_config_example.yaml\"\n"
    "        }\n"
    "    }'\n"
)

# 模块2 中需要按环境修改的片段（高亮显示）
START_CMD_HIGHLIGHTS = (
    "/models/Qwen ...",
    "/workspace/ucm_config_example.yaml",
)

# 模块1 中需要按环境修改的片段（高亮显示）
UCM_CONFIG_HIGHLIGHTS = (
    '"/mnt/test"',
)

# 模块3：验证连通性命令模板（宿主机另开终端执行；端口/模型名动态替换）
VERIFY_CMD_TEMPLATE = (
    "# 1. 服务健康检查\n"
    "curl --fail http://127.0.0.1:7800/health\n"
    "\n"
    "# 2. 模型列表\n"
    "curl --fail http://127.0.0.1:7800/v1/models\n"
    "\n"
    "# 3. 发起一次对话验证推理链路\n"
    "curl --fail-with-body http://127.0.0.1:7800/v1/chat/completions \\\n"
    "    -H 'Content-Type: application/json' \\\n"
    "    -d '{\n"
    "        \"model\": \"qwen3.8\",\n"
    "        \"messages\": [\n"
    "            {\n"
    "                \"role\": \"user\",\n"
    "                \"content\": \"Explain the role of KV cache in large language model inference.\"\n"
    "            }\n"
    "        ],\n"
    "        \"max_completion_tokens\": 512,\n"
    "        \"temperature\": 0,\n"
    "        \"chat_template_kwargs\": {\"enable_thinking\": false}\n"
    "    }'\n"
)

VERIFY_DEFAULT_PORT = "7800"
VERIFY_DEFAULT_MODEL = "qwen3.8"


def _render_highlighted(template: str, highlights) -> str:
    """把模板转成 HTML，对需要修改的片段加高亮（浅黄底），供只读展示。"""
    text = _html.escape(template)
    # 长片段优先替换，避免被短片段抢先命中
    for frag in sorted({h for h in highlights if h}, key=len, reverse=True):
        esc = _html.escape(frag)
        text = text.replace(
            esc, f'<span style="{_HIGHLIGHT_STYLE}">{esc}</span>')
    return text


def build_verify_command(port: str = "", model: str = "") -> str:
    """按端口与模型名生成验证连通性命令；留空时保持模板默认占位值。"""
    port = (port or "").strip() or VERIFY_DEFAULT_PORT
    model = (model or "").strip() or VERIFY_DEFAULT_MODEL
    return (VERIFY_CMD_TEMPLATE
            .replace(VERIFY_DEFAULT_PORT, port)
            .replace(f'"{VERIFY_DEFAULT_MODEL}"', f'"{model}"'))


class ServicePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx

        font = QFont(_MONO_FONT)
        font.setStyleHint(QFont.Monospace)

        def _readonly_view(text: str) -> QPlainTextEdit:
            view = QPlainTextEdit(text)
            view.setReadOnly(True)
            view.setFont(font)
            view.setMinimumHeight(90)
            attach_resize_grip(view)
            return view

        def _readonly_html_view(template: str, highlights) -> QTextEdit:
            """只读富文本视图：需要按环境修改的片段以高亮显示（可复制）。

            用 <pre> 块包裹以保留模板换行——复制出的命令须与原文逐行一致。
            """
            view = QTextEdit()
            view.setReadOnly(True)
            view.setFont(font)
            view.setMinimumHeight(90)
            view.setAcceptRichText(False)
            # <pre> 会把结尾换行渲染成多余空行，先去掉模板尾换行
            text = _render_highlighted(template.rstrip("\n"), highlights)
            view.setHtml(f"<pre>{text}</pre>")
            attach_resize_grip(view)
            return view

        # ---------- 1. UCM 配置文件
        cfg_box = QGroupBox("1. UCM 配置文件")
        lay1 = QVBoxLayout(cfg_box)
        lay1.setSpacing(4)
        cfg_hint = QLabel(
            "UCM 配置文件参考默认配置如下，<b>高亮部分</b>（存储路径等）请按实际环境修改；"
            "将配置保存为 yaml 文件（如 <code>/workspace/ucm_config_example.yaml</code>，"
            "路径可自定义，需与拉起命令中 <code>UCM_CONFIG_FILE</code> 保持一致）；"
            f"详细参数说明见 <a href=\"{UCM_DOC_URL}\">UCM 参数配置说明（华为官方文档）</a>")
        cfg_hint.setTextFormat(Qt.RichText)
        cfg_hint.setOpenExternalLinks(True)
        cfg_hint.setWordWrap(True)
        cfg_hint.setProperty("role", "hint")
        self.cfg_view = _readonly_html_view(UCM_CONFIG_TEMPLATE,
                                            UCM_CONFIG_HIGHLIGHTS)
        lay1.addWidget(cfg_hint)
        lay1.addWidget(self.cfg_view, 1)

        # ---------- 2. 推理服务拉起命令
        cmd_box = QGroupBox("2. 推理服务拉起命令（在容器内执行）")
        lay2 = QVBoxLayout(cmd_box)
        lay2.setSpacing(4)
        cmd_hint = QLabel(
            "注意事项：<br>"
            "① 拉起服务前需先导出环境变量：<code>export ENABLE_UCM_PATCH=1</code>、"
            "<code>export VLLM_CPU_AFFINITY=1</code>；<br>"
            "② 拉起命令后必须附加 <code>--kv-transfer-config</code>，通过 UCMConnector "
            "把 UCM 接入 vLLM；<br>"
            "③ <b>高亮部分</b>需按实际环境修改：模型路径（<code>vllm serve</code> "
            "参数）与 <code>UCM_CONFIG_FILE</code>（指向模块1 保存的 yaml，"
            "两处路径必须一致）。")
        cmd_hint.setTextFormat(Qt.RichText)
        cmd_hint.setWordWrap(True)
        cmd_hint.setProperty("role", "hint")
        self.cmd_view = _readonly_html_view(START_CMD_TEMPLATE,
                                            START_CMD_HIGHLIGHTS)
        lay2.addWidget(cmd_hint)
        lay2.addWidget(self.cmd_view, 1)

        # ---------- 3. 验证连通性（按输入自动生成命令）
        verify_box = QGroupBox("3. 验证连通性（服务就绪后，在宿主机另开终端）")
        lay3 = QVBoxLayout(verify_box)
        lay3.setSpacing(4)
        self.verify_port_edit = QLineEdit(VERIFY_DEFAULT_PORT)
        self.verify_port_edit.setFixedWidth(90)
        self.verify_port_edit.setToolTip(
            "vLLM 服务端口（--port，默认 8000；模板示例为 7800）。\n"
            "修改后下方验证命令自动更新。")
        self.verify_model_edit = QLineEdit()
        self.verify_model_edit.setPlaceholderText("如 qwen3.8（须与 vllm serve 的模型一致）")
        self.verify_model_edit.setToolTip(
            "验证对话时使用的模型名：vLLM 中为 --served-model-name 或\n"
            "模型路径名。留空保持模板示例值。修改后下方命令自动更新。")
        verify_inputs = QHBoxLayout()
        verify_inputs.addWidget(QLabel("端口"))
        verify_inputs.addWidget(self.verify_port_edit)
        verify_inputs.addSpacing(12)
        verify_inputs.addWidget(QLabel("模型名"))
        verify_inputs.addWidget(self.verify_model_edit, 1)
        verify_hint = QLabel(
            "填写端口与模型名后，下方命令自动生成（服务就绪后，在宿主机"
            "另开一个终端执行）。")
        verify_hint.setProperty("role", "hint")
        verify_hint.setWordWrap(True)
        self.verify_view = _readonly_view(build_verify_command())
        self.verify_port_edit.textChanged.connect(self._update_verify_cmd)
        self.verify_model_edit.textChanged.connect(self._update_verify_cmd)
        lay3.addWidget(verify_hint)
        lay3.addLayout(verify_inputs)
        lay3.addWidget(self.verify_view, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)              # 与步骤3/4 一致：模块间顺序感
        layout.addWidget(cfg_box, 1)
        layout.addWidget(cmd_box, 1)
        layout.addWidget(verify_box, 1)

    # ------------------------------------------------------------ 辅助
    def _update_verify_cmd(self) -> None:
        """按当前输入的端口/模型名重新生成验证连通性命令。"""
        self.verify_view.setPlainText(build_verify_command(
            self.verify_port_edit.text(), self.verify_model_edit.text()))

    def on_enter(self) -> None:
        """纯指引页无动态内容，保留钩子以兼容主窗口 on_enter 调用。"""
