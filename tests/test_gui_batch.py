# -*- coding: utf-8 -*-
"""步骤5 批量部署页 GUI 测试（offscreen；多选、模板导入导出、批量执行）。"""
import json
import os
import time

import pytest

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
    from ucm_deployer.gui.widgets.batch_page import BatchPage

    ctx = make_ctx(tmp_path)
    page = BatchPage(ctx)
    page.resize(1000, 860)
    return page


def add_servers(ctx, count=2):
    from ucm_deployer.core.models import ServerInfo

    servers = []
    for i in range(count):
        s = ServerInfo.create(name=f"node-{i + 1}", host=f"10.0.0.1{i + 1}",
                              username="root", password=f"pw{i + 1}")
        ctx.registry.upsert(s)
        servers.append(ctx.registry.get(s.id))
    return servers


def check_target(page, server, qapp, checked=True):
    from PySide6.QtCore import Qt

    for row in range(page.target_table.rowCount()):
        name = page.target_table.item(row, 1).text()
        if name == server.name:
            item = page.target_table.item(row, 0)
            page.target_table.itemChanged.disconnect(page._on_target_item_changed)
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            if checked:
                page._target_ids.add(server.id)
            else:
                page._target_ids.discard(server.id)
            page.target_table.itemChanged.connect(page._on_target_item_changed)
            page._update_target_count()
            return


# ============================================================ 页面与导航
def test_main_window_has_batch_step(qapp):
    from ucm_deployer.gui.main_window import MainWindow

    win = MainWindow()
    win.show()
    qapp.processEvents()
    win.goto_step(4)
    qapp.processEvents()
    assert win.current_step() == 4
    assert win.batch_page is not None
    assert "批量部署" in win._nav_buttons[4].text()
    win.close()


def test_batch_page_fits_one_screen(qapp, tmp_path):
    from ucm_deployer.gui.main_window import MainWindow

    win = MainWindow()
    win.resize(1280, 860)
    win.show()
    qapp.processEvents()
    win.goto_step(5)
    qapp.processEvents()
    page = win.batch_page
    available = win.centralWidget().height()
    # 挂载表加高后允许页面轻微滚动（QScrollArea 包装），只约束不要高得离谱
    # +180：自定义命令 checkbox 单行常驻 + 镜像来源表单「目标保存路径」行
    #（其余编辑区勾选才展开）
    assert page.minimumSizeHint().height() <= available + 180, \
        f"步骤5 最小高度 {page.minimumSizeHint().height()}px 超过可用 {available}px 太多"
    assert page.panel.height() >= 100
    assert page.mount_table.minimumHeight() >= 150, "挂载表应有足够高度展示多行（含空行留白）"
    win.close()


def test_batch_page_multi_select(qapp, tmp_path):
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 3)
    page.refresh_servers()
    qapp.processEvents()

    assert page.target_table.rowCount() == 3
    assert page._selected_targets() == []

    check_target(page, servers[0], qapp)
    check_target(page, servers[2], qapp)
    qapp.processEvents()
    targets = page._selected_targets()
    assert {t.name for t in targets} == {"node-1", "node-3"}
    assert page.target_count_label.text() == "已选 2 台"

    # 全选 / 清空
    page._set_all_targets(True)
    assert len(page._selected_targets()) == 3
    page._set_all_targets(False)
    assert page._selected_targets() == []


def test_batch_page_run_requires_targets(qapp, tmp_path):
    page = make_page(qapp, tmp_path)
    page.image_ref_edit.setText("ucm-vllm:0.3.6")
    page._run()      # 无目标（自动应答提示）后返回
    assert not page.panel.is_running()
    assert page._reports == {}


def test_batch_page_env_column(qapp, tmp_path):
    """「环境」列：步骤1 探测设备的环境预检汇总（PASS/WARN/FAIL/未探测），
    悬浮提示含各项明细。"""
    from ucm_deployer.core.device_detector import HostEnvItem

    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 2)
    page.ctx.env_results[servers[0].id] = [
        HostEnvItem("kernel", "内核版本", "PASS", "5.10.0"),
        HostEnvItem("accelerator_driver", "加速器驱动", "WARN",
                    "HDK=24.1", "升级驱动"),
        HostEnvItem("smmu", "SMMU 开关（宿主机）", "PASS", "未开启"),
    ]
    # FAIL 的服务器
    page.ctx.env_results[servers[1].id] = [
        HostEnvItem("kernel", "内核版本", "FAIL", "4.19.0", "升级内核"),
    ]
    page.refresh_servers()
    qapp.processEvents()

    warn_item = page.target_table.item(0, 5)
    assert warn_item.text() == "WARN"
    assert "内核版本" in warn_item.toolTip() and "5.10.0" in warn_item.toolTip()
    fail_item = page.target_table.item(1, 5)
    assert fail_item.text() == "FAIL"

    # 未探测：灰色占位；探测结果随刷新更新
    page.ctx.env_results.clear()
    page.refresh_servers()
    qapp.processEvents()
    assert page.target_table.item(0, 5).text() == "未探测"

    # 全 PASS 显示 PASS
    page.ctx.env_results[servers[0].id] = [
        HostEnvItem("kernel", "内核版本", "PASS", "5.10.0")]
    page.refresh_servers()
    qapp.processEvents()
    assert page.target_table.item(0, 5).text() == "PASS"


def test_batch_page_on_enter_prefills_from_context(qapp, tmp_path):
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 1)
    page.ctx.selected = [servers[0]]
    page.ctx.images[servers[0].id] = "ucm-vllm:0.3.6"
    page.on_enter()
    qapp.processEvents()
    assert page.image_ref_edit.text() == "ucm-vllm:0.3.6"
    assert page._source_index_of(servers[0].id) == page.source_combo.currentIndex()


# ============================================================ 模板导出/导入
def test_batch_page_template_export_import_roundtrip(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 2)
    page.refresh_servers()
    check_target(page, servers[0], qapp)
    check_target(page, servers[1], qapp)
    page.source_combo.setCurrentIndex(
        page._source_index_of(servers[0].id))

    # 填充各配置
    page.image_ref_edit.setText("ucm-vllm:0.4.0")
    page.recreate_check.setChecked(True)
    page.name_edit.setText("ucm-batch")
    page.tar_remote_edit.setText("/data/imgs")
    page.shm_edit.setText("256g")
    # 统一挂载表：KV Cache 两行 + 模型一行 + 自定义一行（默认空表，逐行添加）
    page.mount_type.setCurrentIndex(0)              # KV Cache
    page.mount_host_edit.setText("/mnt/nvme1")
    page._mount_add()
    page.mount_host_edit.setText("/mnt/nvme2")
    page._mount_add()
    page.mount_type.setCurrentIndex(1)              # 模型
    page.mount_host_edit.setText("/data/models")
    page.mount_container_edit.setText("/models")
    page._mount_add()
    page.mount_type.setCurrentIndex(2)              # 自定义
    page.mount_host_edit.setText("/mnt/share")
    page.mount_container_edit.setText("/share")
    page._mount_add()
    page.mount_edit.setText("/mnt/nvme1")
    page.custom_cfg_check.setChecked(True)
    page.custom_cfg_edit.setPlainText('{"bandwidth": {"workers": 16}}')
    page.stage_precheck_check.setChecked(False)

    path = str(tmp_path / "tpl.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (path, "")))
    page._export_template()
    assert os.path.isfile(path)

    # 新页面（同注册表）导入
    from ucm_deployer.gui.widgets.batch_page import BatchPage

    page2 = BatchPage(make_ctx(tmp_path))
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (path, "")))
    page2._import_template()
    qapp.processEvents()

    assert page2.image_ref_edit.text() == "ucm-vllm:0.4.0"
    assert page2.tar_remote_edit.text() == "/data/imgs"
    assert page2.recreate_check.isChecked() is True
    assert page2.name_edit.text() == "ucm-batch"
    assert page2.shm_edit.text() == "256g"

    def _rows_of(pg, mtype):
        return [r for r in range(pg.mount_table.rowCount())
                if pg.mount_table.item(r, 0).text() == mtype]

    kv_rows = _rows_of(page2, "KV Cache")
    assert [page2.mount_table.item(r, 1).text()
            for r in kv_rows] == ["/mnt/nvme1", "/mnt/nvme2"]
    # KV 行容器内路径锁定为同路径
    for r in kv_rows:
        assert page2.mount_table.item(r, 2).text() == page2.mount_table.item(r, 1).text()
    model_rows = _rows_of(page2, "模型")
    assert len(model_rows) == 1
    assert page2.mount_table.item(model_rows[0], 1).text() == "/data/models"
    assert page2.mount_table.item(model_rows[0], 2).text() == "/models"
    extra_rows = _rows_of(page2, "自定义")
    assert [page2.mount_table.item(r, 1).text() for r in extra_rows] == ["/mnt/share"]
    assert [page2.mount_table.item(r, 2).text() for r in extra_rows] == ["/share"]
    assert page2.mount_edit.text() == "/mnt/nvme1"
    assert page2.custom_cfg_check.isChecked() is True
    assert json.loads(page2.custom_cfg_edit.toPlainText()) == {
        "bandwidth": {"workers": 16}}
    assert page2.stage_precheck_check.isChecked() is False
    # 导入的服务器自动勾选为目标
    assert len(page2._selected_targets()) == 2
    # 源服务器按模板 source_host 匹配选中
    assert page2._selected_source() is not None
    assert page2._selected_source().host == servers[0].host


def test_batch_page_precheck_default_template(qapp, tmp_path):
    """自定义预检配置编辑框预填默认模板（合法 JSON，含常用键）。"""
    from ucm_deployer.gui.widgets.batch_page import DEFAULT_PRECHECK_TEMPLATE

    page = make_page(qapp, tmp_path)
    assert json.loads(page.custom_cfg_edit.toPlainText()) == \
        json.loads(DEFAULT_PRECHECK_TEMPLATE)
    assert "bandwidth" in json.loads(page.custom_cfg_edit.toPlainText())


def test_batch_page_import_empty_custom_cfg_keeps_default(
        qapp, tmp_path, monkeypatch):
    """导入的部署模板未写自定义配置时，编辑框保留默认模板内容。"""
    from PySide6.QtWidgets import QFileDialog

    from ucm_deployer.gui.widgets.batch_page import (
        DEFAULT_PRECHECK_TEMPLATE, BatchPage)

    page = make_page(qapp, tmp_path)
    path = str(tmp_path / "tpl.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (path, "")))
    page._export_template()
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data["precheck"]["custom_config_json"] = ""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    page2 = BatchPage(make_ctx(tmp_path))
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (path, "")))
    page2._import_template()
    qapp.processEvents()
    assert json.loads(page2.custom_cfg_edit.toPlainText()) == \
        json.loads(DEFAULT_PRECHECK_TEMPLATE)


def test_batch_page_import_template_bad_file(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    page = make_page(qapp, tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(bad), "")))
    page._import_template()      # 提示导入失败（自动应答）后返回
    assert page.image_ref_edit.text() == ""


# ============================================================ 批量执行
_NPU_SMI = """+--------------------------------+
| npu-smi 24.1.rc2               |
+===+============+===============+
| NPU     Name   | Health        |
|=================+===============|
| 0       910B3  | OK            |
| 1       910B3  | OK            |
+--------------------------------+"""

_PRECHECK_OUT = (
    "[INFO] serving_stack  vLLM 0.10.0\n"
    "[PASS] kernel  5.10.0\n"
    "[PASS] memory_shm  512 GiB\n"
    "Result: PASSED  |  2 pass, 0 warn, 0 fail\n"
)


def _script_common(fake_ssh):
    fake_ssh.add_default(r"^uname -m", stdout="aarch64\n")
    fake_ssh.add_default(r"^npu-smi info", stdout=_NPU_SMI)
    fake_ssh.add_default(r"for d in /dev/davinci", stdout="/dev/davinci0\n")
    fake_ssh.add_default(r"for p in /usr/local",
                         stdout="/usr/local/Ascend/driver\n")
    fake_ssh.add_default(r"docker run --rm", stdout="uc-manager==1.2.3\n")
    fake_ssh.add_default(r"^docker run -itd", stdout="container-id\n")
    fake_ssh.add_default(r"^docker inspect", stdout="running|0\n")
    fake_ssh.add_default(r"ucm-toolkit run precheck", stdout=_PRECHECK_OUT)


def test_batch_page_ucm_config_write(qapp, tmp_path, fake_ssh):
    """「UCM文件配置」页签：勾选后容器创建完成即写入各目标容器。"""
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 1)
    page.refresh_servers()
    check_target(page, servers[0], qapp)

    fake_ssh.add_default(r"^docker pull", stdout="Status: Downloaded\n")
    fake_ssh.add_default(r"docker image inspect", stdout="")
    # 目标侧存在性检查（--format 形式）：无同名镜像 -> 执行 pull
    fake_ssh.add_default(r"docker image inspect --format", exit_code=1)
    _script_common(fake_ssh)
    # UCM 配置写入链：mkdir -> docker cp -> head 校验 -> rm 临时文件
    fake_ssh.add_default(r"docker exec .* mkdir -p", stdout="")
    fake_ssh.add_default(r"^docker cp", stdout="")
    fake_ssh.add_default(r"docker exec .* head -5",
                         stdout="ucm_connectors:\n  custom: 1\n")
    fake_ssh.add_default(r"^rm -f /tmp/\.ucm-cfg-", stdout="")

    page.mode_combo.setCurrentIndex(1)      # 镜像仓库拉取
    page.image_ref_edit.setText("quay.io/x/ucm-vllm:0.4.0")
    page.ucm_cfg_view.setPlainText("ucm_connectors:\n  custom: 1\n")
    page.ucm_cfg_path_edit.setText("/workspace/ucm_config_example.yaml")
    page._run()
    assert _wait_panel(page.panel, qapp)

    rep = page._reports[servers[0].id]
    assert rep.container == "ucm-vllm"
    cmds = [c.executed_command for c in fake_ssh.instances[0].channels]
    cp = [c for c in cmds if c.startswith("docker cp /tmp/.ucm-cfg-")]
    assert len(cp) == 1
    assert "ucm-vllm:/workspace/ucm_config_example.yaml" in cp[0]
    assert any("head -5 /workspace/ucm_config_example.yaml" in c for c in cmds)
    # 上传内容 = 页签编辑框内容
    up = {k: v for k, v in fake_ssh.instances[0].files.items()
          if k.startswith("/tmp/.ucm-cfg-")}
    assert list(up.values()) == [b"ucm_connectors:\n  custom: 1\n"]
    # 执行说明包含合并后的阶段名（「创建容器并配置UCM文件」默认勾选）
    assert "创建容器并配置UCM文件" in page.exec_summary_label.text()
    assert "写入UCM配置" not in page.exec_summary_label.text()


def test_batch_page_run_pull_mode_two_targets(qapp, tmp_path, fake_ssh):
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 2)
    page.refresh_servers()
    check_target(page, servers[0], qapp)
    check_target(page, servers[1], qapp)

    fake_ssh.add_default(r"^docker pull", stdout="Status: Downloaded\n")
    fake_ssh.add_default(r"docker image inspect", stdout="")
    # 目标侧存在性检查（--format 形式）：无同名镜像 -> 执行 pull
    fake_ssh.add_default(r"docker image inspect --format", exit_code=1)
    _script_common(fake_ssh)
    # UCM 配置写入链（勾选「创建容器并配置 UCM 文件」后每台执行）
    fake_ssh.add_default(r"docker exec .* mkdir -p", stdout="")
    fake_ssh.add_default(r"^docker cp", stdout="")
    fake_ssh.add_default(r"docker exec .* head -5",
                         stdout="ucm_connectors:\n")
    fake_ssh.add_default(r"^rm -f /tmp/\.ucm-cfg-", stdout="")

    page.mode_combo.setCurrentIndex(1)      # 镜像仓库拉取
    page.image_ref_edit.setText("quay.io/x/ucm-vllm:0.4.0")
    page._run()
    assert page.panel.is_running()
    assert _wait_panel(page.panel, qapp)

    # 两台各自完成 pull + docker run + UCM 配置写入 + 预检
    assert set(page._reports) == {s.id for s in servers}
    for rep in page._reports.values():
        assert rep.container == "ucm-vllm"
        assert rep.precheck_verdict == "passed"
    cmds = []
    for client in fake_ssh.instances:
        cmds.extend(c.executed_command for c in client.channels)
    pull_cmds = [c for c in cmds if c.startswith("docker pull")]
    run_cmds = [c for c in cmds if c.startswith("docker run -itd")]
    precheck_cmds = [c for c in cmds if "ucm-toolkit run precheck" in c]
    assert len(pull_cmds) == 2
    assert len(run_cmds) == 2
    assert len(precheck_cmds) == 2
    assert all("quay.io/x/ucm-vllm:0.4.0" in c for c in run_cmds)
    # 状态行/日志按服务器分列：面板为每台目标建了行与页签
    assert page.panel.table.rowCount() == 2
    assert page.panel.tabs.count() == 2


def test_batch_page_run_tar_mode_with_source_row(qapp, tmp_path, fake_ssh):
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 2)      # 0=源，1=目标
    page.refresh_servers()
    check_target(page, servers[1], qapp)

    fake_ssh.add_default(r"docker image inspect", stdout="")
    # 目标侧存在性检查（--format 形式）：无同名镜像 -> 走上传/导入路径
    fake_ssh.add_default(r"docker image inspect --format", exit_code=1)
    fake_ssh.add_default(r"bash -c", stdout="")            # docker save 导出
    fake_ssh.add_default(r"test -s", stdout="")            # 导出文件非空校验
    fake_ssh.add_default(r"rm -f", stdout="")
    fake_ssh.add_default(r"^docker load", stdout="Loaded image: ucm-vllm:0.4.0\n")
    _script_common(fake_ssh)
    # UCM 配置写入链（勾选「创建容器并配置 UCM 文件」后执行）
    fake_ssh.add_default(r"docker exec .* mkdir -p", stdout="")
    fake_ssh.add_default(r"^docker cp", stdout="")
    fake_ssh.add_default(r"docker exec .* head -5",
                         stdout="ucm_connectors:\n")

    # 源服务器选择第一台
    index = page._source_index_of(servers[0].id)
    page.source_combo.setCurrentIndex(index)
    page.image_ref_edit.setText("ucm-vllm:0.4.0")
    page._run()
    assert page.panel.is_running()
    assert _wait_panel(page.panel, qapp)

    # 源 + 目标两行（首行为源服务器准备镜像包）
    assert page.panel.table.rowCount() == 2
    assert page._reports[servers[1].id].image == "ucm-vllm:0.4.0"
    assert page._reports[servers[1].id].precheck_verdict == "passed"

    cmds = []
    for client in fake_ssh.instances:
        cmds.extend(c.executed_command for c in client.channels)
    assert any("docker save -o" in c for c in cmds)
    assert any(c.startswith("docker load -i /tmp/ucm-batch-") for c in cmds)
    assert any(c.startswith("docker run -itd") for c in cmds)
    # 镜像包在本机临时目录（执行完成后已清理）
    assert page._local_tar == ""


def test_batch_page_run_direct_mode(qapp, tmp_path, fake_ssh):
    """服务器直传模式：源服务器 save 导出 → 目标 scp 拉取（sshpass）→
    docker load；全部结束后源端镜像包被清理。"""
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 2)      # 0=源，1=目标
    page.refresh_servers()
    check_target(page, servers[1], qapp)

    fake_ssh.add_default(r"docker image inspect", stdout="")
    # 目标侧存在性检查（--format 形式）：无同名镜像 -> 走 scp/导入路径
    fake_ssh.add_default(r"docker image inspect --format", exit_code=1)
    fake_ssh.add_default(r"bash -c", stdout="")            # docker save 导出
    fake_ssh.add_default(r"test -s", stdout="")            # 导出文件非空校验
    fake_ssh.add_default(r"^which sshpass", stdout="/usr/bin/sshpass\n")
    fake_ssh.add_default(r"^sshpass", stdout="")           # scp 拉取
    fake_ssh.add_default(r"rm -f", stdout="")
    fake_ssh.add_default(r"^docker load", stdout="Loaded image: ucm-vllm:0.4.0\n")
    _script_common(fake_ssh)
    # UCM 配置写入链（「创建容器并配置UCM文件」默认勾选）
    fake_ssh.add_default(r"docker exec .* mkdir -p", stdout="")
    fake_ssh.add_default(r"^docker cp", stdout="")
    fake_ssh.add_default(r"docker exec .* head -5",
                         stdout="ucm_connectors:\n")

    page.mode_combo.setCurrentIndex(2)                     # 服务器直传
    index = page._source_index_of(servers[0].id)
    page.source_combo.setCurrentIndex(index)
    page.image_ref_edit.setText("ucm-vllm:0.4.0")
    page.tar_remote_edit.setText("/data/imgs")
    page._run()
    assert page.panel.is_running()
    assert _wait_panel(page.panel, qapp)

    assert page.panel.table.rowCount() == 2                # 源 + 目标
    assert page._reports[servers[1].id].precheck_verdict == "passed"
    cmds = []
    for client in fake_ssh.instances:
        cmds.extend(c.executed_command for c in client.channels)
    assert any("docker save -o" in c for c in cmds)
    scp_cmds = [c for c in cmds if c.startswith("sshpass")]
    assert len(scp_cmds) == 1
    assert "-P 22" in scp_cmds[0] and "/data/imgs/" in scp_cmds[0]
    assert any(c.startswith("docker load -i /data/imgs/ucm-batch-")
               for c in cmds)
    # 全部结束后源端镜像包被清理（源任务等待目标计数归零后执行）
    assert page._targets_pending <= 0


def test_batch_page_run_tar_source_also_target(qapp, tmp_path, fake_ssh):
    """源服务器同时在目标列表：镜像分发自动跳过，容器/预检照常执行。"""
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 2)      # 0=源+目标，1=目标
    page.refresh_servers()
    check_target(page, servers[0], qapp)
    check_target(page, servers[1], qapp)

    fake_ssh.add_default(r"docker image inspect", stdout="")
    # 目标侧存在性检查（--format 形式）：镜像已存在 -> 跳过分发
    fake_ssh.add_default(r"docker image inspect --format", exit_code=0)
    fake_ssh.add_default(r"bash -c", stdout="")
    fake_ssh.add_default(r"test -s", stdout="")
    fake_ssh.add_default(r"rm -f", stdout="")
    _script_common(fake_ssh)
    # UCM 配置写入链（勾选「创建容器并配置 UCM 文件」后每台执行）
    fake_ssh.add_default(r"docker exec .* mkdir -p", stdout="")
    fake_ssh.add_default(r"^docker cp", stdout="")
    fake_ssh.add_default(r"docker exec .* head -5",
                         stdout="ucm_connectors:\n")

    index = page._source_index_of(servers[0].id)
    page.source_combo.setCurrentIndex(index)
    page.image_ref_edit.setText("ucm-vllm:0.4.0")
    page._run()
    assert _wait_panel(page.panel, qapp)

    # 源服务器两行（准备镜像包 + 自身部署），互不覆盖
    assert page.panel.table.rowCount() == 3
    assert set(page._reports) == {s.id for s in servers}
    # 两台目标的镜像均已存在 -> 自动跳过分发（容器/预检照常执行）
    assert page._reports[servers[0].id].image_skipped is True
    assert page._reports[servers[0].id].container == "ucm-vllm"
    assert page._reports[servers[0].id].precheck_verdict == "passed"
    assert page._reports[servers[1].id].image_skipped is True


def test_batch_page_run_stage_subset_only_image(qapp, tmp_path, fake_ssh):
    """仅勾选「镜像分发」：不创建容器、不预检。"""
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 1)
    page.refresh_servers()
    check_target(page, servers[0], qapp)

    fake_ssh.add_default(r"^docker pull", stdout="Status: Downloaded\n")
    fake_ssh.add_default(r"docker image inspect", stdout="")
    # 目标侧存在性检查（--format 形式）：无同名镜像 -> 执行 pull
    fake_ssh.add_default(r"docker image inspect --format", exit_code=1)

    page.mode_combo.setCurrentIndex(1)
    page.image_ref_edit.setText("quay.io/x/ucm-vllm:0.4.0")
    page.stage_container_check.setChecked(False)
    page.stage_precheck_check.setChecked(False)
    page._run()
    assert _wait_panel(page.panel, qapp)

    rep = page._reports[servers[0].id]
    assert rep.container == ""
    assert rep.precheck_verdict == ""
    cmds = []
    for client in fake_ssh.instances:
        cmds.extend(c.executed_command for c in client.channels)
    assert not any(c.startswith("docker run -itd") for c in cmds)
    assert not any("ucm-toolkit run precheck" in c for c in cmds)


def test_batch_page_custom_command_requires_text(qapp, tmp_path):
    """勾选自定义命令但内容为空：部署前拦截（不启动任务）。"""
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 1)
    page.refresh_servers()
    check_target(page, servers[0], qapp)
    page.image_ref_edit.setText("ucm-vllm:0.4.0")
    page.custom_cmd_check.setChecked(True)     # 展开编辑区但保持为空
    page._run()
    assert not page.panel.is_running()         # 被拦截


def test_batch_page_custom_command_mode(qapp, tmp_path, fake_ssh):
    """勾选自定义命令：整条命令执行（{image} 已替换），跳过逐台设备探测。"""
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 1)
    page.refresh_servers()
    check_target(page, servers[0], qapp)
    fake_ssh.add_default(r"docker run --rm", stdout="uc-manager==1.2.3\n")
    fake_ssh.add_default(r"docker ps", stdout="")
    fake_ssh.add_default(r"^docker run -itd", stdout="container-id\n")
    # UCM 配置写入链（勾选「创建容器并配置 UCM 文件」后执行）
    fake_ssh.add_default(r"docker exec .* mkdir -p", stdout="")
    fake_ssh.add_default(r"^docker cp", stdout="")
    fake_ssh.add_default(r"docker exec .* head -5",
                         stdout="ucm_connectors:\n")
    fake_ssh.add_default(r"^rm -f /tmp/\.ucm-cfg-", stdout="")

    page.image_ref_edit.setText("ucm-vllm:0.4.0")
    page.stage_image_check.setChecked(False)
    page.custom_cmd_check.setChecked(True)
    page.custom_cmd_edit.setPlainText(
        "docker run -itd --name my-ctr --entrypoint /bin/bash "
        "-v /mnt/kv:/mnt/kv {image}")
    page._run()
    assert _wait_panel(page.panel, qapp)
    rep = page._reports[servers[0].id]
    assert rep.container == "my-ctr"

    cmds = []
    for client in fake_ssh.instances:
        cmds.extend(c.executed_command for c in client.channels)
    assert any("ucm-vllm:0.4.0" in c and "my-ctr" in c for c in cmds)
    # 未发生设备探测（无 uname/npu-smi 脚本也成功）
    assert not any("npu-smi info" in c for c in cmds)


def test_batch_page_run_container_precheck_no_source_task(qapp, tmp_path, fake_ssh):
    """tar 模式仅勾「容器创建 + 环境预检」：源服务器不参与执行（修复前
    源服务器仍会 docker save 整个镜像，进度长期卡住且阻塞重新部署）。"""
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 2)
    page.refresh_servers()
    page.source_combo.setCurrentIndex(page._source_index_of(servers[0].id))
    check_target(page, servers[1], qapp)

    fake_ssh.add_default(r"docker run --rm", stdout="uc-manager==1.2.3\n")
    fake_ssh.add_default(r"^uname -m", stdout="aarch64\n")
    fake_ssh.add_default(r"^npu-smi info", stdout=_NPU_SMI)
    fake_ssh.add_default(r"for d in /dev/davinci", stdout="/dev/davinci0\n")
    fake_ssh.add_default(r"for p in /usr/local",
                         stdout="/usr/local/Ascend/driver\n")
    fake_ssh.add_default(r"^docker run -itd", stdout="container-id\n")
    fake_ssh.add_default(r"ucm-toolkit run precheck", stdout=_PRECHECK_OUT)
    # UCM 配置写入链（「创建容器并配置 UCM 文件」默认模板内容）
    fake_ssh.add_default(r"docker exec .* mkdir -p", stdout="")
    fake_ssh.add_default(r"^docker cp", stdout="")
    fake_ssh.add_default(r"docker exec .* head -5",
                         stdout="ucm_connectors:\n")
    fake_ssh.add_default(r"^rm -f /tmp/\.ucm-cfg-", stdout="")

    page.image_ref_edit.setText("ucm-vllm:0.4.0")
    page.stage_image_check.setChecked(False)
    page._run()
    assert _wait_panel(page.panel, qapp)

    rep = page._reports[servers[1].id]
    assert rep.container == "ucm-vllm"
    assert rep.precheck_verdict in ("passed", "completed")
    cmds = []
    for client in fake_ssh.instances:
        cmds.extend(c.executed_command for c in client.channels)
    assert not any("docker save" in c for c in cmds)
    assert not any("bash -c" in c for c in cmds)


def test_edit_resize_grip(qapp, tmp_path):
    """多行输入框右下角拖动手柄：拖动调整高度、三处配置框均已接入。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPlainTextEdit

    from ucm_deployer.gui.widgets.common import _EditGrip, attach_resize_grip

    edit = QPlainTextEdit()
    edit.setFixedHeight(60)
    edit.resize(300, 60)
    grip = attach_resize_grip(edit)
    edit.show()
    qapp.processEvents()
    assert isinstance(grip, _EditGrip)
    assert grip in edit.children()
    assert grip.isVisible()

    # 向下拖 60px -> 高度增大
    QTest.mousePress(grip, Qt.LeftButton)
    QTest.mouseMove(grip, QPoint(grip.width() // 2, grip.height() // 2 + 60))
    QTest.mouseRelease(grip, Qt.LeftButton)
    qapp.processEvents()
    assert edit.height() >= 100

    # 向上拖 300px -> 受 MIN_H 限制
    QTest.mousePress(grip, Qt.LeftButton)
    QTest.mouseMove(grip, QPoint(grip.width() // 2, grip.height() // 2 - 300))
    QTest.mouseRelease(grip, Qt.LeftButton)
    qapp.processEvents()
    assert edit.height() == _EditGrip.MIN_H

    # 步骤5 两个配置框均已接入（步骤3 cmd_edit 由 smoke 用例覆盖构造路径）
    from ucm_deployer.gui.widgets.batch_page import BatchPage

    bp = BatchPage(make_ctx(tmp_path))
    assert any(isinstance(c, _EditGrip) for c in bp.extra_args_edit.children())
    # custom_cfg_edit 改为填满预检页签剩余高度（容器配置页签撑高页签区域，
    # 固定高度会留下大片空白），不再使用拖动手柄
    assert not any(isinstance(c, _EditGrip)
                   for c in bp.custom_cfg_edit.children())


def test_bandwidth_requires_kv_mount(qapp, tmp_path):
    """预检配置：容器配置未添加 KV Cache 目录时「执行存储带宽检查」禁用；
    添加 KV Cache 行后恢复可用，删除后再次禁用并取消勾选。"""
    from ucm_deployer.gui.widgets.batch_page import BatchPage

    bp = BatchPage(make_ctx(tmp_path))
    # 初始（挂载表为空）：禁用、未勾选，带宽目录框同步禁用
    assert not bp.bandwidth_check.isEnabled()
    assert not bp.bandwidth_check.isChecked()
    assert not bp.mount_edit.isEnabled()
    # 添加 KV Cache 行 -> 恢复可用（目录框随勾选状态可用），
    # 带宽目录默认带出第一个 KV Cache 路径（容器内路径与宿主机相同）
    bp.mount_host_edit.setText("/mnt/kv1")
    bp._mount_add()
    qapp.processEvents()
    assert bp.bandwidth_check.isEnabled()
    assert bp.mount_edit.text() == "/mnt/kv1"
    bp.bandwidth_check.setChecked(True)
    assert bp.mount_edit.isEnabled()
    # 添加/删除非 KV 行不影响可用性
    bp.mount_type.setCurrentIndex(2)          # 自定义
    bp.mount_host_edit.setText("/data/other")
    bp.mount_container_edit.setText("/other")
    bp._mount_add()
    qapp.processEvents()
    assert bp.bandwidth_check.isEnabled()
    bp.mount_table.selectRow(1)               # 删除自定义行
    bp._mount_del()
    qapp.processEvents()
    assert bp.bandwidth_check.isEnabled()
    # 删除 KV 行 -> 再次禁用并取消勾选
    bp.mount_table.selectRow(0)
    bp._mount_del()
    qapp.processEvents()
    assert not bp.bandwidth_check.isEnabled()
    assert not bp.bandwidth_check.isChecked()
    assert not bp.mount_edit.isEnabled()


def test_bandwidth_kv_mount_from_template(qapp, tmp_path):
    """模板回填后带宽可用性按容器配置的 KV Cache 挂载重新联动：
    模板含 KV 目录且带宽开启 -> 可用且勾选；模板无 KV 目录 -> 禁用未勾选。"""
    from ucm_deployer.core.batch_deploy import (BatchContainerConfig,
                                                BatchPrecheckConfig,
                                                BatchStages, DeployTemplate)
    from ucm_deployer.gui.widgets.batch_page import BatchPage

    bp = BatchPage(make_ctx(tmp_path))
    bp._apply_template(DeployTemplate(
        image_ref="ucm-engine:v1",
        container=BatchContainerConfig(kv_cache_dirs=["/mnt/kv1"]),
        precheck=BatchPrecheckConfig(bandwidth_check=True,
                                     mount_path="/mnt/kv1"),
        stages=BatchStages()))
    qapp.processEvents()
    assert bp.bandwidth_check.isEnabled()
    assert bp.bandwidth_check.isChecked()
    assert bp.mount_edit.isEnabled()
    assert bp.mount_edit.text() == "/mnt/kv1"

    # 无 KV 目录的模板：带宽禁用且取消勾选（模板里的勾选被联动覆盖）
    bp2 = BatchPage(make_ctx(tmp_path))
    bp2._apply_template(DeployTemplate(
        image_ref="ucm-engine:v1",
        container=BatchContainerConfig(),
        precheck=BatchPrecheckConfig(bandwidth_check=True,
                                     mount_path="/mnt/kv1"),
        stages=BatchStages()))
    qapp.processEvents()
    assert not bp2.bandwidth_check.isEnabled()
    assert not bp2.bandwidth_check.isChecked()
    assert not bp2.mount_edit.isEnabled()


def test_exec_summary_updates(qapp, tmp_path):
    """模块3 动态说明：随阶段开关与目标勾选实时刷新。"""
    from ucm_deployer.gui.widgets.batch_page import BatchPage

    bp = BatchPage(make_ctx(tmp_path))
    servers = add_servers(bp.ctx, 2)
    bp.refresh_servers()
    # 默认全阶段（「创建容器并配置UCM文件」默认勾选）+ 未选目标
    assert bp.exec_summary_label.text() == (
        "已选阶段：分发镜像 → 创建容器并配置UCM文件 → 环境预检"
        "（请先勾选目标服务器）")
    check_target(bp, servers[0], qapp)
    assert bp.exec_summary_label.text() == (
        "将对 1 台目标执行：分发镜像 → 创建容器并配置UCM文件 → 环境预检")
    # 取消各阶段 -> 文案与剩余阶段同步
    bp.stage_image_check.setChecked(False)
    assert bp.exec_summary_label.text() == (
        "将对 1 台目标执行：创建容器并配置UCM文件 → 环境预检")
    bp.stage_container_check.setChecked(False)
    assert bp.exec_summary_label.text() == "将对 1 台目标执行：环境预检"
    bp.stage_precheck_check.setChecked(False)
    assert bp.exec_summary_label.text() == (
        "未勾选任何执行阶段（勾选模块2 右上角的阶段开关）")
    # 页签随勾选置灰/恢复（1=容器配置、3=预检配置；2=UCM文件配置随
    # 「创建容器并配置UCM文件」开关联动）；
    # 0=镜像与分发页签保持可编辑（镜像名始终要填），只禁用分发参数区
    bp.stage_precheck_check.setChecked(False)
    assert not bp.config_tabs.isTabEnabled(3)
    bp.stage_image_check.setChecked(False)
    assert bp.config_tabs.isTabEnabled(0)
    assert not bp._distribute_widget.isEnabled()
    assert bp.image_ref_edit.isEnabled()
    assert "已存在于各目标服务器" in bp._source_hint.text()
    # 「创建容器并配置UCM文件」勾选 -> 容器配置/UCM文件配置可选中
    bp.stage_container_check.setChecked(True)
    assert bp.config_tabs.isTabEnabled(1)
    assert bp.config_tabs.isTabEnabled(2)
    bp.stage_image_check.setChecked(True)
    assert bp._distribute_widget.isEnabled()
    assert bp._source_hint.text() == ""
    bp.stage_precheck_check.setChecked(True)
    assert bp.config_tabs.isTabEnabled(3)
    # 取消「创建容器并配置UCM文件」-> 两页签不可选中、阶段不执行
    bp.stage_container_check.setChecked(False)
    assert not bp.config_tabs.isTabEnabled(1)
    assert not bp.config_tabs.isTabEnabled(2)
    assert not bp._build_stages().create_container
    bp.stage_container_check.setChecked(True)
    assert bp.config_tabs.isTabEnabled(1)
    assert bp.config_tabs.isTabEnabled(2)
    assert bp._build_stages().create_container


def test_target_table_select_interactions(qapp, tmp_path):
    """目标表格勾选交互：点行任意处切换勾选、点「部署」列表头全选/清空、
    勾选行高亮、右键菜单接管全选/清空/刷新（列头为纯文本无三态标记）。"""
    from PySide6.QtCore import Qt

    from ucm_deployer.gui.widgets.batch_page import BatchPage

    bp = BatchPage(make_ctx(tmp_path))
    servers = add_servers(bp.ctx, 3)
    bp.refresh_servers()
    qapp.processEvents()
    header_item = bp.target_table.horizontalHeaderItem(0)
    assert header_item.text() == "部署"       # 列头为纯文本（无三态标记）

    # 点行任意位置（第 1 列）= 勾选该台
    bp.target_table.cellClicked.emit(0, 1)
    qapp.processEvents()
    assert bp._target_ids == {servers[0].id}
    # 勾选行整行高亮
    assert bp.target_table.item(0, 2).background().color().name().lower() \
        in ("#eaf2ff", "#eaf2ff")

    # 再点一次 = 取消（高亮清除，用空画刷而非黑色）
    bp.target_table.cellClicked.emit(0, 1)
    qapp.processEvents()
    assert bp._target_ids == set()
    assert bp.target_table.item(0, 2).background().style() == Qt.NoBrush

    # 点列头 = 全选（当前未选 -> 取反为全选）
    bp.target_table.horizontalHeader().sectionClicked.emit(0)
    qapp.processEvents()
    assert bp._target_ids == {s.id for s in servers}
    assert header_item.text() == "部署"       # 全选后列头仍为纯文本
    # 再点列头 = 清空（已全选 -> 取反）
    bp.target_table.horizontalHeader().sectionClicked.emit(0)
    qapp.processEvents()
    assert bp._target_ids == set()

    # 点击非「部署」列表头不改变勾选
    bp.target_table.cellClicked.emit(1, 1)
    qapp.processEvents()
    bp.target_table.horizontalHeader().sectionClicked.emit(2)
    qapp.processEvents()
    assert bp._target_ids == {servers[1].id}

    # 右键菜单已接管全选/清空/刷新（上下文菜单策略）
    from PySide6.QtCore import Qt as _Qt
    assert bp.target_table.contextMenuPolicy() == _Qt.CustomContextMenu


def test_custom_cmd_disables_extra_args(qapp, tmp_path):
    """两种命令模式互斥的可见反馈：勾选自定义命令 -> 附加参数置灰；
    预检配置同理：未勾选「使用自定义预检配置」时编辑框置灰。"""
    from ucm_deployer.gui.widgets.batch_page import BatchPage

    bp = BatchPage(make_ctx(tmp_path))
    # 初始：附加参数可用，预检编辑框置灰（内容仍可见作参考）
    assert bp.extra_args_edit.isEnabled()
    assert bp.custom_cfg_edit.isEnabled() is False
    assert bp.custom_cfg_edit.toPlainText() != ""
    # 勾选自定义命令 -> 附加参数置灰
    bp.custom_cmd_check.setChecked(True)
    assert not bp.extra_args_edit.isEnabled()
    assert bp.extra_args_edit.toolTip() != ""
    bp.custom_cmd_check.setChecked(False)
    assert bp.extra_args_edit.isEnabled()
    assert bp.extra_args_edit.toolTip() == ""
    # 勾选自定义预检配置 -> 编辑框启用
    bp.custom_cfg_check.setChecked(True)
    assert bp.custom_cfg_edit.isEnabled()
    bp.custom_cfg_check.setChecked(False)
    assert not bp.custom_cfg_edit.isEnabled()


def test_batch_page_mount_verify(qapp, tmp_path, fake_ssh):
    """批量校验挂载路径：对勾选目标并行 test -d，结果写入各日志页签。"""
    page = make_page(qapp, tmp_path)
    servers = add_servers(page.ctx, 2)
    page.refresh_servers()
    check_target(page, servers[0], qapp)
    check_target(page, servers[1], qapp)
    fake_ssh.add_default(r"^test -d /mnt/exists", stdout="")
    fake_ssh.add_default(r"^test -d /mnt/missing", exit_code=1)
    page.mount_host_edit.setText("/mnt/exists")
    page._mount_add()
    page.mount_host_edit.setText("/mnt/missing")
    page._mount_add()
    page._mount_verify()
    assert page.panel.is_running()
    assert _wait_panel(page.panel, qapp)
    cmds = []
    for client in fake_ssh.instances:
        cmds.extend(c.executed_command for c in client.channels)
    assert cmds.count("test -d /mnt/exists") == 2      # 2 台目标各查 1 次
    assert cmds.count("test -d /mnt/missing") == 2
    text = "\n".join(w.toPlainText()
                     for w in page.panel._logs.values())
    assert "✓ 存在：/mnt/exists" in text
    assert "✗ 不存在：/mnt/missing" in text
    assert "mkdir -p /mnt/missing" in text


def test_batch_page_mount_verify_guards(qapp, tmp_path, fake_ssh):
    """批量校验守卫：空挂载表 / 未勾选目标直接提示，不产生 SSH 命令。"""
    page = make_page(qapp, tmp_path)
    add_servers(page.ctx, 1)
    page.refresh_servers()
    page._mount_verify()          # 空挂载表 -> 提示
    assert not page.panel.is_running()
    page.mount_host_edit.setText("/mnt/kv1")
    page._mount_add()
    page._mount_verify()          # 未勾选目标 -> 提示
    assert not page.panel.is_running()
    for client in fake_ssh.instances:
        assert not any(c.executed_command.startswith("test -d")
                       for c in client.channels)
