# -*- coding: utf-8 -*-
"""步骤5 推理服务部署页 GUI 测试（offscreen；命令指引/路径联动/连通性自动执行）。"""
import time

import pytest
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

# offscreen 下 QMessageBox 会模态阻塞等待点击——全部替换为直接返回
from PySide6.QtWidgets import QMessageBox            # noqa: E402

for _name in ("information", "warning", "critical", "about"):
    setattr(QMessageBox, _name,
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
QMessageBox.question = staticmethod(                 # noqa: A001
    lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.exec = lambda self, *a, **k: QMessageBox.StandardButton.Ok  # noqa: A001


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _shutdown_tasks_after_test(qapp):
    yield
    from PySide6.QtWidgets import QApplication

    from ucm_deployer.gui.widgets.common import ParallelTaskPanel, _task_threads
    for w in QApplication.topLevelWidgets():
        for panel in w.findChildren(ParallelTaskPanel):
            panel.shutdown()
    for t in list(_task_threads):
        t.join(15.0)
    qapp.processEvents()


def _wait_panel(panel, qapp, timeout=60):
    deadline = time.time() + timeout
    while panel.is_running() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.05)
    qapp.processEvents()
    return not panel.is_running()


def make_ctx(tmp_path):
    from ucm_deployer.core import crypto
    from ucm_deployer.core.server_registry import ServerRegistry
    from ucm_deployer.gui.state import AppContext
    from ucm_deployer.utils import paths

    paths.default_config_dir = lambda: tmp_path
    crypto._key_path = lambda: tmp_path / ".secret.key"
    return AppContext(ServerRegistry(tmp_path))


def make_page(qapp, tmp_path):
    from ucm_deployer.gui.widgets.service_page import ServicePage

    page = ServicePage(make_ctx(tmp_path))
    page.resize(1000, 860)
    return page


# ============================================================ 指引内容
def test_service_page_no_config_module(qapp, tmp_path):
    """UCM 配置写入已移至步骤3「UCM 文件配置」：本页无 cfg 组件。"""
    page = make_page(qapp, tmp_path)
    assert not hasattr(page, "cfg_view")
    assert not hasattr(page, "cfg_write_btn")
    assert not hasattr(page, "cfg_path_edit")
    assert not hasattr(page, "cfg_container_edit")
    assert not hasattr(page, "_write_config")
    cmd = page.cmd_view.toPlainText()
    assert "UCM_CONFIG_FILE" in cmd


def test_service_page_start_cmd_guide(qapp, tmp_path):
    """模块1：拉起命令模板含环境变量导出与 --kv-transfer-config 接入块。"""
    page = make_page(qapp, tmp_path)
    cmd = page.cmd_view.toPlainText()
    assert "export ENABLE_UCM_PATCH=1" in cmd
    assert "export VLLM_CPU_AFFINITY=1" in cmd
    assert "--kv-transfer-config" in cmd
    assert '"kv_connector": "UCMConnector"' in cmd
    assert '"kv_connector_module_path": "ucm.integration.vllm.ucm_connector"' in cmd
    assert '"kv_role": "kv_both"' in cmd
    assert "UCM_CONFIG_FILE" in cmd
    assert "/workspace/ucm_config_example.yaml" in cmd
    assert page.cmd_view.isReadOnly()


def test_service_page_verify_guide(qapp, tmp_path):
    """模块2：连通性验证模板（health / models / chat completions）。"""
    page = make_page(qapp, tmp_path)
    verify = page.verify_view.toPlainText()
    assert "curl --fail http://127.0.0.1:7800/health" in verify
    assert "curl --fail http://127.0.0.1:7800/v1/models" in verify
    assert "curl --fail-with-body http://127.0.0.1:7800/v1/chat/completions" in verify
    assert '"max_completion_tokens": 512' in verify
    assert '"enable_thinking": false' in verify
    assert page.verify_view.isReadOnly()


# ============================================================ 高亮与编辑定位
def test_service_page_highlights_placeholders(qapp, tmp_path):
    """拉起命令需修改片段高亮展示、复制仍是原文。"""
    from PySide6.QtWidgets import QTextEdit

    from ucm_deployer.gui.widgets.service_page import (
        CFG_DEFAULT_PATH, START_CMD_HIGHLIGHTS, START_CMD_TEMPLATE,
        _render_highlighted)

    page = make_page(qapp, tmp_path)
    assert isinstance(page.cmd_view, QTextEdit)
    # 复制内容 = 模板原文（高亮只影响展示样式，不改变文本）
    assert page.cmd_view.toPlainText() == START_CMD_TEMPLATE.rstrip("\n")
    assert CFG_DEFAULT_PATH in START_CMD_TEMPLATE
    # 富文本包含高亮底色（Qt toHtml 会重排样式，以颜色值为准）
    assert "#fef3c7" in page.cmd_view.toHtml()
    for frag in START_CMD_HIGHLIGHTS:
        assert _render_highlighted(
            frag, START_CMD_HIGHLIGHTS).count("span") >= 2
    # 纯函数：模板未含片段时不高亮
    assert "span" not in _render_highlighted("plain text", ["nope"])


def test_service_page_verify_command_generation(qapp, tmp_path):
    """模块3：按输入的服务器 IP/端口/模型名自动生成验证命令；留空保持模板占位值。"""
    from ucm_deployer.gui.widgets.service_page import (
        VERIFY_CMD_TEMPLATE, VERIFY_DEFAULT_HOST, VERIFY_DEFAULT_MODEL,
        VERIFY_DEFAULT_PORT, build_verify_command)

    # 纯函数：默认值
    cmd = build_verify_command()
    assert cmd == VERIFY_CMD_TEMPLATE
    assert f"127.0.0.1:{VERIFY_DEFAULT_PORT}/health" in cmd
    assert f'"model": "{VERIFY_DEFAULT_MODEL}"' in cmd
    # 纯函数：主机、端口与模型名替换
    cmd = build_verify_command("192.168.1.50", "9000", "glm-9b")
    assert "192.168.1.50:9000/health" in cmd
    assert '"model": "glm-9b"' in cmd
    assert "7800" not in cmd and "qwen3.8" not in cmd
    # 纯函数：空白输入回退默认
    assert "7800/health" in build_verify_command("  ", "", "")
    # UI 联动：输入变化实时更新命令文本
    page = make_page(qapp, tmp_path)
    assert "7800/health" in page.verify_view.toPlainText()
    page.verify_ip_edit.setText("10.0.0.8")
    page.verify_port_edit.setText("9000")
    page.verify_model_edit.setText("glm-9b")
    verify = page.verify_view.toPlainText()
    assert "10.0.0.8:9000/health" in verify
    assert "10.0.0.8:9000/v1/models" in verify
    assert '"model": "glm-9b"' in verify
    assert "7800" not in verify
    # 自动测试使用同一地址
    assert page.verify_ip_edit.placeholderText().startswith("127.0.0.1")
def test_service_page_widgets(qapp, tmp_path):
    """页面组件：连通性可自动执行；无部署按钮/容器选择/配置写入组件。"""
    from PySide6.QtWidgets import QComboBox, QPushButton

    page = make_page(qapp, tmp_path)
    btn_texts = [b.text() for b in page.findChildren(QPushButton)]
    assert not any("部署" in t or "🚀" in t for t in btn_texts)
    assert not page.findChildren(QComboBox)
    # 执行入口：连通性测试按钮 + 任务面板（配置写入在步骤3）
    assert hasattr(page, "panel")
    assert hasattr(page, "verify_btn")
    assert not hasattr(page, "cfg_write_btn")
    assert not hasattr(page, "deploy_btn")


def test_service_page_grips_attachable(qapp, tmp_path):
    """两个展示区均可通过右下角手柄拖动调高。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from ucm_deployer.gui.widgets.common import _EditGrip

    page = make_page(qapp, tmp_path)
    page.show()
    qapp.processEvents()
    page.verify_view.setFixedHeight(90)
    qapp.processEvents()
    h0 = page.verify_view.height()
    grip = next(c for c in page.verify_view.children()
                if isinstance(c, _EditGrip))
    QTest.mousePress(grip, Qt.LeftButton)
    QTest.mouseMove(grip, QPoint(grip.width() // 2, grip.height() // 2 + 40))
    QTest.mouseRelease(grip, Qt.LeftButton)
    qapp.processEvents()
    assert page.verify_view.height() >= h0 + 40
    page.close()


# ============================================================ 路径联动
def test_service_page_config_path_sync(qapp, tmp_path):
    """步骤3 写入的 UCM 配置路径 → 拉起命令 UCM_CONFIG_FILE 同步。"""
    from ucm_deployer.gui.widgets.service_page import CFG_DEFAULT_PATH

    page = make_page(qapp, tmp_path)
    assert f'"{CFG_DEFAULT_PATH}"' in page.cmd_view.toPlainText()
    # 直接调用联动（模拟步骤3 写入后的通知）
    page._sync_cmd_path("/opt/my/ucm.yaml")
    cmd = page.cmd_view.toPlainText()
    assert '"UCM_CONFIG_FILE": "/opt/my/ucm.yaml"' in cmd
    assert CFG_DEFAULT_PATH not in cmd
    # 新路径仍以高亮富文本展示
    assert "#fef3c7" in page.cmd_view.toHtml()
    # 空白路径 → 回退默认值
    page._sync_cmd_path("  ")
    assert f'"{CFG_DEFAULT_PATH}"' in page.cmd_view.toPlainText()


def test_service_page_config_path_from_ctx(qapp, tmp_path):
    """on_enter 时按 ctx.ucm_config_paths 记录同步拉起命令。"""
    page = make_page(qapp, tmp_path)
    server = _make_server(tmp_path)
    page.ctx.registry.upsert(server)
    page.ctx.selected = [server]
    page.ctx.ucm_config_paths[server.id] = "/opt/from-step3/ucm.yaml"
    page.on_enter()
    cmd = page.cmd_view.toPlainText()
    assert '"UCM_CONFIG_FILE": "/opt/from-step3/ucm.yaml"' in cmd
    assert "#fef3c7" in page.cmd_view.toHtml()


# ============================================================ 写入容器
def _make_server(tmp_path):
    """造一台已注册服务器并设为当前选定。"""
    from ucm_deployer.core.models import ServerInfo

    s = ServerInfo.create(name="node-1", host="10.0.0.11",
                          username="root", password="pw1")
    return s


def test_service_page_write_config_to_container(qapp, tmp_path, fake_ssh):
    """写入容器已移至步骤3：service 页无该功能（迁移标记测试）。"""
    page = make_page(qapp, tmp_path)
    assert not hasattr(page, "_write_config")
    page.close()


# ============================================================ 连通性自动测试
def test_service_page_verify_auto_pass(qapp, tmp_path, fake_ssh):
    """连通性测试：三条 curl 全 PASS，日志含各项结果与汇总。"""
    from ucm_deployer.gui.widgets.service_page import ServicePage

    from ucm_deployer.core import crypto
    from ucm_deployer.utils import paths

    paths.default_config_dir = lambda: tmp_path
    crypto._key_path = lambda: tmp_path / ".secret.key"
    ctx = make_ctx(tmp_path)
    s = _make_server(tmp_path)
    ctx.registry.upsert(s)
    ctx.selected = [s]

    page = ServicePage(ctx)
    fake_ssh.add_default(r"curl --fail -s -o /dev/null", stdout="200")
    fake_ssh.add_default(r"curl.*v1/models",
                         stdout='{"data":[{"id":"qwen3.8"}]}\n')
    fake_ssh.add_default(r"curl.*chat/completions",
                         stdout='{"choices":[{"message":{"role":"assistant"}}]}\n')

    page._run_verify()
    assert page.panel.is_running()
    assert _wait_panel(page.panel, qapp)

    client = fake_ssh.instances[0]
    cmds = [c.executed_command for c in client.channels]
    assert sum(1 for c in cmds if c.startswith("curl")) == 3
    assert any("v1/chat/completions" in c and "-H 'Content-Type: application/json'" in c
               for c in cmds)
    assert any('"model": "qwen3.8"' in c for c in cmds)
    logs = page.panel.tabs.widget(0).toPlainText() \
        if page.panel.tabs.count() else ""
    assert "PASS：服务健康检查" in logs
    assert "PASS：模型列表" in logs
    assert "PASS：对话验证" in logs
    assert "全部通过" in logs
    page.close()


def test_service_page_verify_auto_fail(qapp, tmp_path, fake_ssh):
    """连通性测试：服务未就绪 → FAIL 项 + 汇总失败 + 排查提示。"""
    from ucm_deployer.gui.widgets.service_page import ServicePage

    from ucm_deployer.core import crypto
    from ucm_deployer.utils import paths

    paths.default_config_dir = lambda: tmp_path
    crypto._key_path = lambda: tmp_path / ".secret.key"
    ctx = make_ctx(tmp_path)
    s = _make_server(tmp_path)
    ctx.registry.upsert(s)
    ctx.selected = [s]

    page = ServicePage(ctx)
    # 仅 health 通，models/chat 未注册（exit 127 = 连接失败）
    fake_ssh.add_default(r"curl --fail -s -o /dev/null", stdout="200")

    page._run_verify()
    assert _wait_panel(page.panel, qapp)

    logs = page.panel.tabs.widget(0).toPlainText() \
        if page.panel.tabs.count() else ""
    assert "FAIL：模型列表" in logs
    assert "FAIL：对话验证" in logs
    assert "排查提示" in logs
    # 失败汇总显示在面板状态列
    status = page.panel.table.item(0, 2).text()
    assert "2/3 项未通过" in status
    page.close()


# ============================================================ 主窗口集成
def test_service_page_in_main_window(qapp, tmp_path, fake_ssh):
    """主窗口集成：步骤5 为推理服务拉起页；任务面板清单包含本页。"""
    from ucm_deployer.gui.main_window import MainWindow

    fake_ssh.add_default(r"docker ps", stdout="")
    win = MainWindow()
    win.resize(1280, 860)
    win.show()
    qapp.processEvents()
    assert len(win._nav_buttons) == 6
    assert win._nav_buttons[4].text().startswith("（可选）批量部署")
    assert win._nav_buttons[5].text().startswith("步骤5：推理服务部署")
    win.goto_step(5)
    qapp.processEvents()
    assert win.current_step() == 5
    panels = win._all_panels()
    assert len(panels) == 6
    win.close()
