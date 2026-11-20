# -*- coding: utf-8 -*-
from ucm_deployer.core.device_detector import (
    DeviceDetector,
    HostEnvItem,
    check_host_env,
    summarize_env_status,
)
from ucm_deployer.core.models import DeviceType, ServerInfo
from ucm_deployer.core.ssh_client import SSHClient

_NPU_SMI = """+--------------------------------+
| npu-smi 24.1.rc2               |
+===+============+===============+
| NPU     Name   | Health        |
|=================+===============|
| 0       910B3  | OK            |
| 1       910B3  | OK            |
| 2       910B3  | OK            |
| 3       910B3  | OK            |
+--------------------------------+"""

_NPU_SMI_950 = """+------------------------------------------------------------------------------------------------------------------+
| npu-smi 25.6.rc1.b999                            Version: 25.6.rc1.b999                                          |
+--------+------------------+---------------+----------------------------------------------------------------------+
| NPU ID | Name             | Health        | Power(W)              Temp(C)                  Hugepages-Usage(page) |
        |                  | Bus-Id        | NPU Util(%)           Memory-Usage(MB)         HBM-Usage(MB)          |
+========+==================+===============+======================================================================+
| 0      | Ascend950PR      | OK            | 270.2                 45                       0     / 0             |
        |                  | NA            | 0                     0                        5249  / 131072          |
+------------------------------------------------------------------------------------------------------------------+"""


def make_ssh(fake_ssh) -> SSHClient:
    ssh = SSHClient(ServerInfo.create(name="s", host="127.0.0.1"))
    ssh.connect()
    return ssh


def test_detect_ascend_by_npu_smi(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^npu-smi info", stdout=_NPU_SMI)
    dev = DeviceDetector(ssh).detect()
    assert dev.device_type == DeviceType.ASCEND
    assert dev.model == "Ascend 910B3"
    assert dev.count == 4


def test_detect_ascend_950_pr_by_npu_smi(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^npu-smi info", stdout=_NPU_SMI_950)
    dev = DeviceDetector(ssh).detect()
    assert dev.device_type == DeviceType.ASCEND
    assert dev.model == "Ascend950PR"
    assert dev.count == 1


def test_detect_ascend_by_dev_nodes(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^npu-smi info", exit_code=127)
    ssh._client.add(r"ls -1 /dev/ .*grep -cE.*davinci", stdout="16\n")
    dev = DeviceDetector(ssh).detect()
    assert dev.device_type == DeviceType.ASCEND
    assert dev.count == 16


def test_detect_nvidia(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^npu-smi info", exit_code=127)
    ssh._client.add(r"^nvidia-smi --query-gpu",
                    stdout="0, NVIDIA A800-SXM4-80GB\n1, NVIDIA A800-SXM4-80GB\n")
    dev = DeviceDetector(ssh).detect()
    assert dev.device_type == DeviceType.NVIDIA
    assert dev.model == "NVIDIA A800-SXM4-80GB"
    assert dev.count == 2


def test_detect_none(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^npu-smi info", exit_code=127)
    ssh._client.add(r"^nvidia-smi", exit_code=127)
    ssh._client.add(r"^lspci", stdout="0\n")
    dev = DeviceDetector(ssh).detect()
    assert dev.device_type == DeviceType.UNKNOWN
    assert dev.count == 0


def test_detect_arch(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^uname -m", stdout="aarch64\n")
    assert DeviceDetector(ssh).detect_arch() == "aarch64"


def test_detect_arch_unsupported(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^uname -m", stdout="riscv64\n")
    assert DeviceDetector(ssh).detect_arch() == ""


def test_detect_includes_arch(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^npu-smi info", stdout=_NPU_SMI)
    ssh._client.add(r"^uname -m", stdout="aarch64\n")
    dev = DeviceDetector(ssh).detect()
    assert dev.arch == "aarch64"


# ============================================================ 宿主机环境预检
def test_host_env_all_pass(fake_ssh):
    """内核 5.10+、Ascend HDK 25.6（>25.2）、SMMU 未开启 -> 全 PASS。"""
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^uname -r", stdout="5.10.0-1.el8.aarch64\n")
    ssh._client.add(r"^cat /usr/local/Ascend/driver/version.info",
                    stdout="Version=25.6.rc1.b999\n")
    ssh._client.add(r"^dmesg >/dev/null", exit_code=0)
    items = check_host_env(ssh)
    by_key = {i.key: i for i in items}
    assert set(by_key) == {"kernel", "accelerator_driver", "smmu"}
    assert by_key["kernel"].status == "PASS"
    assert "5.10" in by_key["kernel"].summary
    assert by_key["accelerator_driver"].status == "PASS"
    assert by_key["accelerator_driver"].summary == "HDK=25.6.rc1.b999"
    assert by_key["smmu"].status == "PASS"


def test_host_env_kernel_too_old_and_hdk_low(fake_ssh):
    """内核 4.19 -> FAIL；HDK 24.1（不高于 25.2）-> WARN。"""
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^uname -r", stdout="4.19.90-xxx\n")
    ssh._client.add(r"^cat /usr/local/Ascend/driver/version.info",
                    stdout="Version=24.1.rc1\n")
    ssh._client.add(r"^dmesg >/dev/null", exit_code=0)
    items = check_host_env(ssh)
    by_key = {i.key: i for i in items}
    assert by_key["kernel"].status == "FAIL"
    assert "5.10" in by_key["kernel"].advice
    assert by_key["accelerator_driver"].status == "WARN"
    assert "25.2" in by_key["accelerator_driver"].advice


def test_host_env_nvidia_driver(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^uname -r", stdout="5.15.0-generic\n")
    ssh._client.add(r"^cat /usr/local/Ascend/driver/version.info", exit_code=1)
    ssh._client.add(r"^npu-smi info", exit_code=127)
    ssh._client.add(r"^nvidia-smi --query-gpu=driver_version",
                    stdout="580.65.06\n")
    ssh._client.add(r"^dmesg >/dev/null", exit_code=0)
    items = check_host_env(ssh)
    by_key = {i.key: i for i in items}
    assert by_key["accelerator_driver"].status == "PASS"
    assert by_key["accelerator_driver"].summary == "driver=580.65.06"


def test_host_env_smmu_enabled_warns(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^uname -r", stdout="5.10.0\n")
    ssh._client.add(r"^dmesg >/dev/null", exit_code=0)
    ssh._client.add(r"^dmesg \| grep",
                    stdout="arm-smmu-v3 device already in use\n")
    items = check_host_env(ssh)
    by_key = {i.key: i for i in items}
    assert by_key["smmu"].status == "WARN"
    assert "BIOS" in by_key["smmu"].advice


def test_host_env_unavailable_items_skip(fake_ssh):
    """命令全部失败 -> SKIP（不误报 FAIL）；dmesg 权限受限也 SKIP。"""
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^uname -r", exit_code=1)
    ssh._client.add(r"^dmesg >/dev/null", exit_code=1)
    items = check_host_env(ssh)
    by_key = {i.key: i for i in items}
    assert by_key["kernel"].status == "SKIP"
    assert by_key["accelerator_driver"].status == "SKIP"
    assert by_key["smmu"].status == "SKIP"


# ============================================================ 环境状态汇总
def test_summarize_env_status():
    def item(status):
        return HostEnvItem(key="x", name="x", status=status)

    assert summarize_env_status([]) == ""
    assert summarize_env_status([item("PASS"), item("PASS")]) == "PASS"
    assert summarize_env_status([item("PASS"), item("WARN")]) == "WARN"
    assert summarize_env_status([item("WARN"), item("FAIL")]) == "FAIL"
    assert summarize_env_status([item("SKIP")]) == ""       # 无结论
    assert summarize_env_status([item("SKIP"), item("PASS")]) == "PASS"
