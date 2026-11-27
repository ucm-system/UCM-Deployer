# -*- coding: utf-8 -*-
"""步骤5：推理服务部署 —— 拉起命令指引 + 自动验证页。

页面分两个模块（阅读顺序 = 操作顺序）：
1. 推理服务拉起命令：容器内执行的命令模板，包含必需的环境变量导出
   （ENABLE_UCM_PATCH / VLLM_CPU_AFFINITY）与 --kv-transfer-config
   接入 UCMConnector 的配置块；需要修改的占位（模型路径、UCM_CONFIG_FILE）
   以高亮显示——UCM_CONFIG_FILE 路径与步骤3「UCM 文件配置」写入的
   路径自动联动；
2. 验证连通性：按输入的服务器 IP、端口与模型名自动生成 curl 命令
   （可复制到宿主机另开终端执行），也可点击「执行连通性测试」由工具
   通过 SSH 在目标服务器上自动执行并逐项判定。

UCM 配置文件的编辑与写入已在步骤3「UCM 文件配置」模块完成。
唯一执行入口：连通性测试（通过任务面板后台执行）。
"""
from __future__ import annotations

import html as _html
import json as _json

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.container_manager import UCM_CONFIG_DEFAULT_PATH
from ..state import AppContext
from .common import ParallelTaskPanel, attach_resize_grip

_MONO_FONT = "Consolas"

# 高亮样式：需要用户按环境修改的片段（浅黄底 + 深棕字 + 加粗）
_HIGHLIGHT_STYLE = "background-color:#fef3c7; color:#b45309; font-weight:bold"

# UCM 参数配置说明（华为支持文档，界面中可点击跳转）
UCM_DOC_URL = ("https://support.huawei.com/enterprise/zh/doc/EDOC1100582752/"
               "b4b5937b?idPath=7919749%7C251366268%7C250389224%7C"
               "263153904%7C268354339")

# UCM 配置文件在容器内的默认保存路径（步骤3 写入；拉起命令模板引用）
CFG_DEFAULT_PATH = UCM_CONFIG_DEFAULT_PATH

# 模块1：推理服务拉起命令模板（容器内执行）
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
    "            \"UCM_CONFIG_FILE\": \"" + CFG_DEFAULT_PATH + "\"\n"
    "        }\n"
    "    }'\n"
)

# 模块1 中需要按环境修改的片段（高亮显示；UCM_CONFIG_FILE 值随步骤3 写入路径联动）
START_CMD_HIGHLIGHTS = (
    CFG_DEFAULT_PATH,
)

# 模块2：验证连通性命令模板（宿主机另开终端执行；端口/模型名动态替换）
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
VERIFY_DEFAULT_HOST = "127.0.0.1"
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


def build_verify_command(host: str = "", port: str = "",
                         model: str = "") -> str:
    """按主机、端口与模型名生成验证连通性命令；留空时保持模板默认占位值。"""
    host = (host or "").strip() or VERIFY_DEFAULT_HOST
    port = (port or "").strip() or VERIFY_DEFAULT_PORT
    model = (model or "").strip() or VERIFY_DEFAULT_MODEL
    return (VERIFY_CMD_TEMPLATE
            .replace(f"http://{VERIFY_DEFAULT_HOST}:{VERIFY_DEFAULT_PORT}",
                     f"http://{host}:{port}")
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

        def _readonly_html_view(template: str, highlights,
                                fit_content: bool = False) -> QTextEdit:
            """只读富文本视图：需要按环境修改的片段以高亮显示（可复制）。

            用 <pre> 块包裹以保留模板换行——复制出的命令须与原文逐行一致。
            fit_content=True 时高度贴合模板内容（固定行数的配置模板不做
            弹性拉伸，避免编辑框下方留白；右下角手柄仍可手动调整）。
            """
            view = QTextEdit()
            view.setReadOnly(True)
            view.setFont(font)
            view.setMinimumHeight(90)
            view.setAcceptRichText(False)
            # <pre> 会把结尾换行渲染成多余空行，先去掉模板尾换行
            text = _render_highlighted(template.rstrip("\n"), highlights)
            view.setHtml(f"<pre>{text}</pre>")
            if fit_content:
                # 未显示时文档未布局（size 为 0），先给定宽度触发布局；
                # <pre> 不折行，高度与宽度无关
                view.document().setTextWidth(600)
                view.setFixedHeight(
                    int(view.document().size().height()) + 16)
            attach_resize_grip(view)
            return view

        # ---------- 1. 推理服务拉起命令
        cmd_box = QGroupBox("1. 推理服务拉起命令（在容器内执行）")
        lay1 = QVBoxLayout(cmd_box)
        lay1.setSpacing(4)
        cmd_hint = QLabel(
            "注意事项：<br>"
            "① 拉起服务前需先导出环境变量：<code>export ENABLE_UCM_PATCH=1</code>、"
            "<code>export VLLM_CPU_AFFINITY=1</code>；<br>"
            "② 拉起命令后必须附加 <code>--kv-transfer-config</code>，通过 UCMConnector "
            "把 UCM 接入 vLLM；<br>"
            "③ <b>PD 分离部署</b>时，<code>--kv-transfer-config</code> 只需在 "
            "<b>Prefill 实例</b>的拉起命令中添加，<b>Decode 实例无需添加</b>；"
            "单机 / 多机部署按原有方式（即下方模板）。<br>"
            "④ <b>高亮部分</b>需按实际环境修改：模型路径（<code>vllm serve</code> "
            "参数）与 <code>UCM_CONFIG_FILE</code>（指向步骤3「UCM 文件配置」"
            f"写入的 yaml，写入成功后自动同步；"
            "详细参数说明见步骤3 页内链接）。")
        cmd_hint.setTextFormat(Qt.RichText)
        cmd_hint.setWordWrap(True)
        cmd_hint.setProperty("role", "hint")
        # 拉起命令模板：UCM_CONFIG_FILE 的值随步骤3「UCM 文件配置」写入路径联动
        self._cmd_path = CFG_DEFAULT_PATH
        self.cmd_view = _readonly_html_view(START_CMD_TEMPLATE,
                                            (self._cmd_path,))
        lay1.addWidget(cmd_hint)
        lay1.addWidget(self.cmd_view, 1)

        # ---------- 2. 验证连通性（按输入自动生成命令）
        verify_box = QGroupBox("2. 验证连通性（服务就绪后，在宿主机另开终端）")
        lay2 = QVBoxLayout(verify_box)
        lay2.setSpacing(4)
        self.verify_ip_edit = QLineEdit()
        self.verify_ip_edit.setPlaceholderText(VERIFY_DEFAULT_HOST + "（默认本机）")
        self.verify_ip_edit.setFixedWidth(190)
        self.verify_ip_edit.setToolTip(
            "验证命令指向的服务器 IP / 主机名：在服务器本机执行时留空\n"
            "（即 127.0.0.1）；从其他机器执行时填目标服务器 IP。\n"
            "修改后下方验证命令自动更新，「执行连通性测试」同样使用该地址\n"
            "（测试经由 SSH 在目标服务器上执行，通常留空即可）。")
        self.verify_port_edit = QLineEdit(VERIFY_DEFAULT_PORT)
        self.verify_port_edit.setFixedWidth(90)
        self.verify_port_edit.setToolTip(
            "vLLM 服务端口（--port，默认 8000；模板示例为 7800）。\n"
            "修改后下方验证命令自动更新。")
        self.verify_model_edit = QLineEdit(VERIFY_DEFAULT_MODEL)
        self.verify_model_edit.setFixedWidth(220)
        self.verify_model_edit.setToolTip(
            "验证对话时使用的模型名：vLLM 中为 --served-model-name 或\n"
            "模型路径名，须与 vllm serve 的模型一致。修改后下方命令自动更新。")
        self.verify_btn = QPushButton("▶ 执行连通性测试")
        self.verify_btn.setToolTip(
            "通过 SSH 在目标服务器上自动执行下方三条 curl 命令\n"
            "并逐项判定（PASS/FAIL 结果写入任务日志）。")
        self.verify_btn.clicked.connect(self._run_verify)
        verify_inputs = QHBoxLayout()
        verify_inputs.addWidget(QLabel("服务器 IP"))
        verify_inputs.addWidget(self.verify_ip_edit)
        verify_inputs.addSpacing(12)
        verify_inputs.addWidget(QLabel("端口"))
        verify_inputs.addWidget(self.verify_port_edit)
        verify_inputs.addSpacing(12)
        verify_inputs.addWidget(QLabel("模型名"))
        verify_inputs.addWidget(self.verify_model_edit)
        verify_inputs.addSpacing(12)
        verify_inputs.addWidget(self.verify_btn)
        verify_inputs.addStretch(1)
        verify_hint = QLabel(
            "填写端口与模型名后，下方命令自动生成。可复制到宿主机另开的终端执行，"
            "或点击「执行连通性测试」由工具通过 SSH 自动执行。")
        verify_hint.setProperty("role", "hint")
        verify_hint.setWordWrap(True)
        self.verify_view = _readonly_view(build_verify_command())
        self.verify_ip_edit.textChanged.connect(self._update_verify_cmd)
        self.verify_port_edit.textChanged.connect(self._update_verify_cmd)
        self.verify_model_edit.textChanged.connect(self._update_verify_cmd)
        # 连通性测试任务面板（单服务器执行）
        self.panel = ParallelTaskPanel(self)
        lay2.addWidget(verify_hint)
        lay2.addLayout(verify_inputs)
        lay2.addWidget(self.verify_view, 1)
        lay2.addWidget(self.panel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)              # 与步骤3/4 一致：模块间顺序感
        layout.addWidget(cmd_box, 1)
        layout.addWidget(verify_box, 1)

    # ------------------------------------------------------------ 辅助
    def _sync_cmd_path(self, path: str) -> None:
        """步骤3「UCM 文件配置」写入路径变化时，同步拉起命令中的
        UCM_CONFIG_FILE 并更新高亮。"""
        path = (path or "").strip() or CFG_DEFAULT_PATH
        if path == self._cmd_path:
            return
        template = START_CMD_TEMPLATE.replace(self._cmd_path, path)
        self._cmd_path = path
        # <pre> 会把结尾换行渲染成多余空行，与 _readonly_html_view 保持一致
        self.cmd_view.setHtml("<pre>"
                              + _render_highlighted(template.rstrip("\n"),
                                                    (path,))
                              + "</pre>")

    def _run_verify(self) -> None:
        """通过 SSH 在目标服务器上执行三条 curl，逐项判定连通性。"""
        port = self.verify_port_edit.text().strip() or VERIFY_DEFAULT_PORT
        model = self.verify_model_edit.text().strip() or VERIFY_DEFAULT_MODEL
        if not self.ctx.selected:
            QMessageBox.information(
                self, "提示", "请先在步骤1 选定目标服务器，再执行连通性测试")
            return
        server = self.ctx.selected[0]
        host = self.verify_ip_edit.text().strip() or VERIFY_DEFAULT_HOST
        base = f"http://{host}:{port}"
        # JSON 载荷单行传递；单引号按 POSIX 规则转义，避免破坏 shell 引号
        payload = _json.dumps({
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "Explain the role of KV cache in large "
                               "language model inference.",
                },
            ],
            "max_completion_tokens": 512,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }, ensure_ascii=False).replace("'", "'\\''")
        checks = [
            ("服务健康检查 /health",
             f"curl --fail -s -o /dev/null -w '%{{http_code}}' {base}/health"),
            ("模型列表 /v1/models",
             f"curl --fail -s {base}/v1/models"),
            ("对话验证 /v1/chat/completions",
             f"curl --fail-with-body -s {base}/v1/chat/completions "
             f"-H 'Content-Type: application/json' -d '{payload}'"),
        ]

        def fn(ssh, tctx):
            tctx.log(f"在 {server.endpoint} 上执行连通性测试"
                     f"（端口 {port}，模型 {model}）")
            tctx.log("提示：服务需以 --network host 或端口映射方式暴露端口")
            fails = 0
            total = len(checks)
            for i, (name, cmd) in enumerate(checks, 1):
                if tctx.cancelled():
                    raise RuntimeError("已取消")
                tctx.progress(int(100 * (i - 1) / total), f"{name} ...")
                tctx.log(f"[{i}/{total}] {name}")
                res = ssh.exec(cmd, timeout=60)
                if res.ok:
                    out = res.stdout.strip()
                    if i == 1 and out.isdigit():
                        tctx.log(f"  → HTTP {out}")
                    elif out:
                        preview = out if len(out) <= 160 else out[:160] + " ..."
                        tctx.log(f"  → {preview}")
                    tctx.log(f"  ✅ PASS：{name}")
                else:
                    fails += 1
                    detail = (res.stderr.strip() or res.stdout.strip()
                              or f"退出码 {res.exit_code}")
                    tctx.log(f"  ❌ FAIL：{name}（{detail}）")
            if fails:
                tctx.log("排查提示：服务是否已就绪 / 端口是否一致 / "
                         "容器是否 --network host；可在容器内 "
                         f"curl 127.0.0.1:{port}/health 进一步定位")
                raise RuntimeError(f"{fails}/{total} 项未通过")
            tctx.log(f"✅ 连通性测试 {total}/{total} 全部通过")
            tctx.progress(100, "连通性测试全部通过 ✅")

        self.panel.run_tasks([(server, fn)], "连通性测试（目标服务器上执行）")

    def _update_verify_cmd(self) -> None:
        """按当前输入的服务器 IP/端口/模型名重新生成验证连通性命令。"""
        self.verify_view.setPlainText(build_verify_command(
            self.verify_ip_edit.text(), self.verify_port_edit.text(),
            self.verify_model_edit.text()))

    def on_enter(self) -> None:
        """进入页面时按步骤3 写入的 UCM 配置路径同步拉起命令模板。"""
        server = self.ctx.server
        if server is None:
            return
        path = self.ctx.ucm_config_paths.get(server.id)
        if path:
            self._sync_cmd_path(path)
