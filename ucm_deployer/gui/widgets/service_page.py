# -*- coding: utf-8 -*-
"""步骤5：服务部署 —— 纯操作指引页。

页面分三个模块（阅读顺序 = 操作顺序）：
1. UCM 配置文件：参考默认配置 + 参数说明官方网址（可点击）；
2. 推理服务拉起命令：容器内执行的命令模板，包含必需的环境变量导出
   （ENABLE_UCM_PATCH / VLLM_CPU_AFFINITY）与 --kv-transfer-config
   接入 UCMConnector 的配置块；
3. 验证连通性：服务就绪后在宿主机另开终端用 curl 验证的命令模板。

本页为静态指引（展示 + 可复制），不含任何执行按钮。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..state import AppContext
from .common import attach_resize_grip

_MONO_FONT = "Consolas"

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

# 模块3：验证连通性命令模板（宿主机另开终端执行）
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

        # ---------- 1. UCM 配置文件
        cfg_box = QGroupBox("1. UCM 配置文件")
        lay1 = QVBoxLayout(cfg_box)
        lay1.setSpacing(4)
        cfg_hint = QLabel(
            "UCM 配置文件参考默认配置如下，存储路径 / 容量 / 并发等参数请按实际环境修改；"
            "将配置保存为 yaml 文件（如 <code>/workspace/ucm_config_example.yaml</code>，"
            "路径可自定义，需与拉起命令中 <code>UCM_CONFIG_FILE</code> 保持一致）；"
            f"详细参数说明见 <a href=\"{UCM_DOC_URL}\">UCM 参数配置说明（华为官方文档）</a>")
        cfg_hint.setTextFormat(Qt.RichText)
        cfg_hint.setOpenExternalLinks(True)
        cfg_hint.setWordWrap(True)
        cfg_hint.setProperty("role", "hint")
        self.cfg_view = _readonly_view(UCM_CONFIG_TEMPLATE)
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
            "③ 其中 <code>UCM_CONFIG_FILE</code> 为 UCM 配置文件路径，指向模块1 保存的 "
            "yaml（可自定义修改，两处路径必须一致）。")
        cmd_hint.setTextFormat(Qt.RichText)
        cmd_hint.setWordWrap(True)
        cmd_hint.setProperty("role", "hint")
        self.cmd_view = _readonly_view(START_CMD_TEMPLATE)
        lay2.addWidget(cmd_hint)
        lay2.addWidget(self.cmd_view, 1)

        # ---------- 3. 验证连通性
        verify_box = QGroupBox("3. 验证连通性（服务就绪后，在宿主机另开终端）")
        lay3 = QVBoxLayout(verify_box)
        lay3.setSpacing(4)
        verify_hint = QLabel(
            "服务就绪后，打开第二个宿主机终端执行以下命令；"
            "端口 <code>7800</code> 与模型名 <code>qwen3.8</code> 按实际替换。")
        verify_hint.setTextFormat(Qt.RichText)
        verify_hint.setWordWrap(True)
        verify_hint.setProperty("role", "hint")
        self.verify_view = _readonly_view(VERIFY_CMD_TEMPLATE)
        lay3.addWidget(verify_hint)
        lay3.addWidget(self.verify_view, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)              # 与步骤3/4 一致：模块间顺序感
        layout.addWidget(cfg_box, 1)
        layout.addWidget(cmd_box, 1)
        layout.addWidget(verify_box, 1)

    # ------------------------------------------------------------ 辅助
    def on_enter(self) -> None:
        """纯指引页无动态内容，保留钩子以兼容主窗口 on_enter 调用。"""
