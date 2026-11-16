# -*- coding: utf-8 -*-
"""步骤5 服务部署页 GUI 测试（offscreen；校验拦截、部署流程、日志面板）。"""
import time

import pytest

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


def _automate_message_boxes() -> None:
    from PySide6.QtWidgets import QMessageBox

    for name in ("information", "warning", "critical", "about"):
        setattr(QMessageBox, name,
                staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.exec = lambda self, *a, **k: QMessageBox.StandardButton.Ok  # noqa: A001


_automate_message_boxes()


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

    ctx = make_ctx(tmp_path)
    page = ServicePage(ctx)
    page.resize(1000, 860)
    return page


def make_server(ctx):
    from ucm_deployer.core.models import ServerInfo

    s = ServerInfo.create(name="node-1", host="10.0.0.11",
                          username="root", password="pw")
    ctx.registry.upsert(s)
    server = ctx.registry.get(s.id)
    ctx.selected = [server]          # ctx.server = selected[0]（只读 property）
    return server


def _script_pipeline(ssh, content_len, service_log="service started\n"):
    """部署流水线 fake 返回（实例级，LIFO）。"""
    ssh._client.add(r"^docker inspect", stdout="true\n")
    ssh._client.add(r"base64 -d", stdout="")
    ssh._client.add(r"wc -c", stdout=f"{content_len}\n")
    ssh._client.add(r"nohup", stdout="12345\n")
    ssh._client.add(r"tail -n", stdout=service_log)


def test_service_page_requires_login(qapp, tmp_path):
    page = make_page(qapp, tmp_path)
    page._run()          # 未登录 -> 拦截
    assert page.status_label.text() == "未部署"
    assert not page.panel.isHidden()   # 日志区常驻（父页面未 show 也成立）


def test_service_page_requires_container_and_content(qapp, tmp_path):
    page = make_page(qapp, tmp_path)
    make_server(page.ctx)
    page.container_combo.setCurrentText("ucm-ctr")
    page.content_edit.setPlainText("")     # 模板默认有预填内容，显式清空
    page._run()          # 配置内容为空 -> 拦截
    assert not page.panel.is_running()


def test_service_page_deploy_flow(qapp, tmp_path, fake_ssh):
    page = make_page(qapp, tmp_path)
    server = make_server(page.ctx)
    page.container_combo.setCurrentText("ucm-ctr")
    page.path_edit.setText("/etc/ucm/config.yaml")
    page.content_edit.setPlainText("ucm:\n  kv_cache: /mnt/kvcache\n")
    page.start_edit.setPlainText("vllm serve /m --ucm-config /etc/ucm/config.yaml")

    content_len = len(page.content_edit.toPlainText().encode("utf-8"))
    # docker ps 容器列表快任务 + 部署流水线
    ssh = None
    from ucm_deployer.core.ssh_client import SSHClient

    class _Probe:
        pass

    fake_ssh.add_default(r"docker ps", stdout="ucm-ctr\n")
    # 部署脚本：连接后才注册实例脚本——用 default_queue 表达流水线时序
    fake_ssh.add_default(r"^docker inspect", stdout="true\n")
    fake_ssh.add_default(r"base64 -d", stdout="")
    fake_ssh.add_default(r"wc -c", stdout=f"{content_len}\n")
    fake_ssh.add_default(r"nohup", stdout="12345\n")
    fake_ssh.add_default(r"tail -n", stdout="service started\n")

    page._run()
    assert not page.panel.isHidden()          # 日志区常驻可见
    assert _wait_panel(page.panel, qapp)
    assert "部署完成" in page.status_label.text()

    cmds = []
    for client in fake_ssh.instances:
        cmds.extend(c.executed_command for c in client.channels)
    assert any("base64 -d > /etc/ucm/config.yaml" in c for c in cmds)
    assert any("nohup vllm serve" in c for c in cmds)
    assert any("tail -n 100" in c for c in cmds)


def test_service_page_grip_only_resizes_itself(qapp, tmp_path):
    """拖动配置框手柄：只改变自身高度，命令框与其他模块高度不变。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    page = make_page(qapp, tmp_path)
    page.show()
    qapp.processEvents()
    page.tabs.setCurrentIndex(0)       # 配置页签为当前页，手柄可交互
    page.content_edit.setFixedHeight(90)
    qapp.processEvents()
    h0 = page.content_edit.height()

    from ucm_deployer.gui.widgets.common import _EditGrip
    grip = next(c for c in page.content_edit.children()
                if isinstance(c, _EditGrip))
    QTest.mousePress(grip, Qt.LeftButton)
    QTest.mouseMove(grip, QPoint(grip.width() // 2, grip.height() // 2 + 40))
    QTest.mouseRelease(grip, Qt.LeftButton)
    qapp.processEvents()

    assert page.content_edit.height() >= h0 + 40
    assert not page.panel.is_running()


# ============================================================ 部署配置默认值
def test_service_page_default_configs(qapp, tmp_path):
    """双页签默认值：UCM 配置模板预填 + 拉起命令空白 + 文档链接可跳转。"""
    page = make_page(qapp, tmp_path)
    assert page.tabs.count() == 2
    assert page.tabs.tabText(0) == "UCM 配置文件"
    assert page.tabs.tabText(1) == "推理服务拉起命令"
    # UCM 配置默认模板
    content = page.content_edit.toPlainText()
    assert "ucm_connectors:" in content
    assert 'ucm_connector_name: "UcmPipelineStore"' in content
    assert 'store_pipeline: "Cache|Posix"' in content
    assert "storage_backends: \"/mnt/test\"" in content
    assert "posix_capacity_gb: 10240" in content
    assert "posix_io_engine: \"psync\"" in content
    assert "posix_data_trans_concurrency: 128" in content
    # 拉起命令默认为空（placeholder 引导）
    assert page.start_edit.toPlainText() == ""
    # 文档链接（可点击跳转）
    from ucm_deployer.gui.widgets.service_page import UCM_DOC_URL

    assert "support.huawei.com" in UCM_DOC_URL


def test_service_page_in_main_window(qapp, tmp_path, fake_ssh):
    """主窗口集成：步骤5 导航到服务部署页，批量部署顺延为第 6 项。"""
    from ucm_deployer.gui.main_window import MainWindow

    fake_ssh.add_default(r"docker ps", stdout="")
    win = MainWindow()
    win.resize(1280, 860)
    win.show()
    qapp.processEvents()
    assert len(win._nav_buttons) == 6
    assert win._nav_buttons[4].text().startswith("步骤5：服务部署")
    assert win._nav_buttons[5].text().startswith("（可选）批量部署")
    win.goto_step(4)
    qapp.processEvents()
    assert win.current_step() == 4
    assert win.service_page is not None
    win.close()
