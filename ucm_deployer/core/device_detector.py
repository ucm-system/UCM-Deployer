# -*- coding: utf-8 -*-
"""设备探测：Ascend NPU / NVIDIA GPU 识别、卡数、CPU 架构，及宿主机环境预检。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .models import DeviceInfo, DeviceType
from .precheck import PrecheckRunner, _ASCEND_HDK_THRESHOLD
from .ssh_client import SSHClient

_NPU_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|?\s*([A-Za-z0-9][A-Za-z0-9\-]*)\s*\|\s*(OK|Warning|Alarm|Fault)\s*\|",
    re.M,
)
# 驱动版本解析口径与容器内预检一致（precheck.py）
_ASCEND_DRIVER_FILE_RE = re.compile(r"(?im)^\s*Version\s*=\s*([^\s#]+)")
_NPU_SMI_VERSION_RE = re.compile(
    r"(?im)(?:npu-smi\s+|Version:\s*)(\d+(?:\.[0-9A-Za-z.-]+)+)")
_NVIDIA_DRIVER_RE = re.compile(r"(?im)^\s*(\d+(?:\.\d+)+)\s*$")
_KERNEL_MIN = (5, 10)      # 与容器内预检一致（major.minor 比较）


@dataclass
class HostEnvItem:
    """宿主机环境预检项（步骤1 探测设备时提前执行）。"""

    key: str            # kernel / accelerator_driver / smmu
    name: str           # 中文名
    status: str         # PASS / WARN / FAIL / SKIP
    summary: str = ""
    advice: str = ""


def _version_key(version: str) -> tuple:
    numbers = []
    for part in re.split(r"[._-]", version):
        if not part.isdigit():
            break
        numbers.append(int(part))
    return tuple(numbers)


def check_host_env(ssh: SSHClient) -> List[HostEnvItem]:
    """宿主机环境预检：内核版本 / 加速器驱动 / SMMU。

    与步骤4 容器内预检同一套判定口径（内核 ≥ 5.10、Ascend HDK > 25.2.0、
    dmesg 中不应出现 arm-smmu-v3），提前到步骤1 执行，目的是在准备镜像、
    创建容器之前先确认服务器基础环境可用。
    """
    items: List[HostEnvItem] = []

    # ---- 内核版本
    res = ssh.exec("uname -r", timeout=15)
    kernel = (res.stdout or "").strip() if res.ok else ""
    match = re.match(r"(\d+)\.(\d+)", kernel)
    if match and (int(match.group(1)), int(match.group(2))) >= _KERNEL_MIN:
        items.append(HostEnvItem(
            "kernel", "内核版本", "PASS", kernel,
            f"threshold: >= {'.'.join(map(str, _KERNEL_MIN))}"))
    elif kernel:
        items.append(HostEnvItem(
            "kernel", "内核版本", "FAIL", kernel,
            "升级操作系统内核至 5.10 及以上系列（按 major.minor 比较）。"))
    else:
        items.append(HostEnvItem(
            "kernel", "内核版本", "SKIP", "无法获取（uname -r 失败）"))

    # ---- 加速器驱动（宿主机直接探测，解析口径与预检 facts 一致）
    driver_version = ""
    driver_source = ""
    res = ssh.exec(
        "cat /usr/local/Ascend/driver/version.info 2>/dev/null", timeout=15)
    if res.ok:
        match = _ASCEND_DRIVER_FILE_RE.search(res.stdout or "")
        if match is not None:
            driver_version = match.group(1)
            driver_source = "/usr/local/Ascend/driver/version.info"
    if not driver_version:
        res = ssh.exec("npu-smi info 2>/dev/null", timeout=60)
        if res.ok:
            match = _NPU_SMI_VERSION_RE.search(res.stdout or "")
            if match is not None:
                driver_version = match.group(1)
                driver_source = "npu-smi info"
    if not driver_version:
        res = ssh.exec(
            "nvidia-smi --query-gpu=driver_version --format=csv,noheader"
            " 2>/dev/null | head -1", timeout=30)
        if res.ok:
            match = _NVIDIA_DRIVER_RE.search(res.stdout or "")
            if match is not None:
                driver_version = match.group(1)
                driver_source = "nvidia-smi"
    if driver_version:
        if driver_source == "nvidia-smi":
            items.append(HostEnvItem(
                "accelerator_driver", "加速器驱动", "PASS",
                f"driver={driver_version}", ""))
        else:
            ok = _version_key(driver_version) > _ASCEND_HDK_THRESHOLD
            items.append(HostEnvItem(
                "accelerator_driver", "加速器驱动",
                "PASS" if ok else "WARN",
                f"HDK={driver_version}",
                "" if ok else
                "Ascend HDK 版本过低（预期 > 25.2.0），请升级 NPU 驱动固件。"))
    else:
        items.append(HostEnvItem(
            "accelerator_driver", "加速器驱动", "SKIP",
            "未能识别驱动版本（可手动执行 npu-smi info / nvidia-smi 确认）"))

    # ---- SMMU（复用预检的宿主机检查，逻辑/口径完全一致）
    try:
        smmu = PrecheckRunner(ssh).check_smmu_host()
        items.append(HostEnvItem(
            "smmu", "SMMU 开关（宿主机）", smmu.status,
            smmu.summary,
            smmu.advice if smmu.status in ("WARN", "FAIL") else ""))
    except Exception:
        items.append(HostEnvItem(
            "smmu", "SMMU 开关（宿主机）", "SKIP",
            "检查失败（详见任务日志）"))
    return items


def summarize_env_status(items: List["HostEnvItem"]) -> str:
    """汇总环境预检结果：FAIL > WARN > PASS；无数据/无结论返回 ''。"""
    if not items:
        return ""
    statuses = {it.status for it in items}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    if "PASS" in statuses:
        return "PASS"
    return ""


class DeviceDetector:
    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def detect(self) -> DeviceInfo:
        """按 Ascend -> NVIDIA 顺序探测，并附带 CPU 架构（uname -m）。"""
        info = self._detect_ascend()
        if info is None or info.count <= 0:
            info = self._detect_nvidia()
        if info is None or info.count <= 0:
            info = DeviceInfo()
        info.arch = self.detect_arch()
        return info

    def detect_arch(self) -> str:
        """CPU 架构（uname -m）；仅返回 x86_64 / aarch64，失败返回 ''。"""
        res = self.ssh.exec("uname -m", timeout=15)
        if not res.ok:
            return ""
        arch = (res.stdout or "").strip().lower()
        return arch if arch in ("x86_64", "aarch64") else ""

    def _detect_ascend(self) -> Optional[DeviceInfo]:
        res = self.ssh.exec("npu-smi info", timeout=60)
        if not res.ok:
            return self._detect_ascend_by_dev()
        rows = _NPU_ROW.findall(res.stdout)
        if not rows:
            return self._detect_ascend_by_dev()
        ids = sorted({int(r[0]) for r in rows})
        raw_model = rows[0][1]
        model = raw_model if re.match(r"(?i)^(ascend|atlas)", raw_model) else f"Ascend {raw_model}"
        return DeviceInfo(DeviceType.ASCEND, model, len(ids), detail=res.stdout[:4000])

    def _detect_ascend_by_dev(self) -> Optional[DeviceInfo]:
        res = self.ssh.exec(
            "ls -1 /dev/ 2>/dev/null | grep -cE '^davinci[0-9]+$'", timeout=30)
        if res.ok:
            try:
                count = int(res.stdout.strip())
            except ValueError:
                return None
            if count > 0:
                return DeviceInfo(DeviceType.ASCEND, "Ascend NPU(型号未知)", count,
                                  detail="通过 /dev/davinci* 探测")
        return None

    def _detect_nvidia(self) -> Optional[DeviceInfo]:
        res = self.ssh.exec(
            "nvidia-smi --query-gpu=index,name --format=csv,noheader", timeout=60)
        if res.ok and res.stdout.strip():
            names = []
            for line in res.stdout.strip().splitlines():
                if not line.strip():
                    continue
                parts = line.split(",", 1)
                names.append(parts[1].strip() if len(parts) == 2 else line.strip())
            if names:
                return DeviceInfo(DeviceType.NVIDIA, names[0], len(names),
                                  detail=res.stdout[:4000])
        res2 = self.ssh.exec("lspci 2>/dev/null | grep -ci nvidia", timeout=30)
        if res2.ok:
            try:
                count = int(res2.stdout.strip())
            except ValueError:
                count = 0
            if count > 0:
                return DeviceInfo(DeviceType.NVIDIA, "NVIDIA GPU(型号未知)", count,
                                  detail="通过 lspci 探测")
        return None
