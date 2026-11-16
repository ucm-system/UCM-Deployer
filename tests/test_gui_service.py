# -*- coding: utf-8 -*-
"""步骤5 服务部署页 GUI 测试（offscreen；纯指引页：三模块内容与无执行组件）。"""
import pytest
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


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
def test_service_page_ucm_config_guide(qapp, tmp_path):
    """模块1：UCM 配置参考模板 + 官方参数文档网址。"""
    from ucm_deployer.gui.widgets.service_page import UCM_DOC_URL

    page = make_page(qapp, tmp_path)
    content = page.cfg_view.toPlainText()
    assert "ucm_connectors:" in content
    assert 'ucm_connector_name: "UcmPipelineStore"' in content
    assert 'store_pipeline: "Cache|Posix"' in content
    assert "storage_backends: \"/mnt/test\"" in content
    assert "posix_capacity_gb: 10240" in content
    assert "posix_io_engine: \"psync\"" in content
    assert "posix_data_trans_concurrency: 128" in content
    assert page.cfg_view.isReadOnly()
    # 参考网址可点击跳转
    assert "support.huawei.com" in UCM_DOC_URL
    labels = " ".join(l.text() for l in page.findChildren(type(page))) or ""
    from PySide6.QtWidgets import QLabel

    hint_texts = " ".join(l.text() for l in page.findChildren(QLabel))
    assert UCM_DOC_URL in hint_texts


def test_service_page_start_cmd_guide(qapp, tmp_path):
    """模块2：拉起命令模板含环境变量导出与 --kv-transfer-config 接入块。"""
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
    """模块3：连通性验证模板（health / models / chat completions）。"""
    page = make_page(qapp, tmp_path)
    verify = page.verify_view.toPlainText()
    assert "curl --fail http://127.0.0.1:7800/health" in verify
    assert "curl --fail http://127.0.0.1:7800/v1/models" in verify
    assert "curl --fail-with-body http://127.0.0.1:7800/v1/chat/completions" in verify
    assert '"max_completion_tokens": 512' in verify
    assert '"enable_thinking": false' in verify
    assert page.verify_view.isReadOnly()


# ============================================================ 纯指引页定位
def test_service_page_has_no_exec_widgets(qapp, tmp_path):
    """纯指引页：无部署按钮/容器选择/日志面板等执行组件。"""
    from PySide6.QtWidgets import QComboBox, QPushButton, QTabWidget

    page = make_page(qapp, tmp_path)
    btn_texts = [b.text() for b in page.findChildren(QPushButton)]
    assert not any("部署" in t or "🚀" in t for t in btn_texts)
    assert not page.findChildren(QComboBox)
    assert not page.findChildren(QTabWidget)
    assert not hasattr(page, "panel")
    assert not hasattr(page, "deploy_btn")


def test_service_page_grips_attachable(qapp, tmp_path):
    """三个展示区均可通过右下角手柄拖动调高。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from ucm_deployer.gui.widgets.common import _EditGrip

    page = make_page(qapp, tmp_path)
    page.show()
    qapp.processEvents()
    page.cfg_view.setFixedHeight(90)
    qapp.processEvents()
    h0 = page.cfg_view.height()
    grip = next(c for c in page.cfg_view.children()
                if isinstance(c, _EditGrip))
    QTest.mousePress(grip, Qt.LeftButton)
    QTest.mouseMove(grip, QPoint(grip.width() // 2, grip.height() // 2 + 40))
    QTest.mouseRelease(grip, Qt.LeftButton)
    qapp.processEvents()
    assert page.cfg_view.height() >= h0 + 40
    page.close()


# ============================================================ 主窗口集成
def test_service_page_in_main_window(qapp, tmp_path, fake_ssh):
    """主窗口集成：步骤5 为服务部署指引页；任务面板清单不再包含本页。"""
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
    assert not hasattr(win.service_page, "panel")
    panels = win._all_panels()
    assert len(panels) == 5
    assert not hasattr(panels[0].parent(), "service_page") or all(
        p is not None for p in panels)
    win.close()
