# -*- coding: utf-8 -*-
"""设备探测：Ascend NPU / NVIDIA GPU 识别、卡数、CPU 架构，及宿主机环境检测。"""
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
_CANN_MIN = (8, 0)         # CANN Toolkit 版本下限（major.minor）
_MEM_MIN_GB = 32           # 宿主机内存下限（GB）
_DOCKER_MIN = (20, 10)     # Docker 版本下限（major.minor）
_SHM_MIN_GB = 512          # /dev/shm 共享内存下限（GB）
_AIO_PER_CARD = 4096       # 每张推理卡要求的可用 AIO 资源


@dataclass
class HostEnvItem:
    """宿主机环境检测项（步骤1「环境检测」时执行）。"""

    key: str            # kernel / accelerator_driver / smmu / cann / memory \
    #                     / docker / shm / aio
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


def check_host_env(ssh: SSHClient,
                   card_count: Optional[int] = None) -> List[HostEnvItem]:
    """宿主机环境检测：内核版本 / 加速器驱动 / SMMU / CANN / 内存 / Docker
    / 共享内存 / AIO 可用资源。

    与步骤4 容器内复检同一套判定口径（内核 ≥ 5.10、Ascend HDK > 25.2.0、
    dmesg 中不应出现 arm-smmu-v3），提前到步骤1 执行，目的是在准备镜像、
    创建容器之前先确认服务器基础环境可用；另附 CANN 版本、内存大小与
    Docker 版本检查（指定范围：CANN ≥ 8.0、内存 ≥ 32GB、Docker ≥ 20.10）；
    共享内存（/dev/shm）须 ≥ 512GB；可用 AIO 资源须 ≥ 推理卡数 × 4096
    （card_count 缺省时跳过该项）。
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

    # ---- CANN 版本（Ascend 服务器；容器方式部署宿主机可无 CANN -> SKIP）
    is_ascend = bool(driver_version) and driver_source != "nvidia-smi"
    cann_version = ""
    res = ssh.exec(
        "head -5 /usr/local/Ascend/ascend_toolkit_install.info 2>/dev/null",
        timeout=15)
    if res.ok:
        match = re.search(r"version[=:]\s*([0-9][0-9A-Za-z._-]*)",
                          res.stdout or "")
        if match is not None:
            cann_version = match.group(1).rstrip(".")
    if not cann_version:
        res = ssh.exec(
            "ls -1 /usr/local/Ascend/ascend-toolkit 2>/dev/null"
            " | grep -Ev 'latest' | tail -1", timeout=15)
        cand = (res.stdout or "").strip()
        if res.ok and re.match(r"^\d", cand):
            cann_version = cand
    if cann_version:
        ok = _version_key(cann_version) >= _CANN_MIN
        items.append(HostEnvItem(
            "cann", "CANN 版本",
            "PASS" if ok else "WARN",
            f"CANN={cann_version}",
            "" if ok else
            "CANN 版本低于 "
            + '.'.join(map(str, _CANN_MIN))
            + " 预期范围，请确认容器镜像内 CANN 版本满足推理框架要求。"))
    elif is_ascend:
        items.append(HostEnvItem(
            "cann", "CANN 版本", "SKIP",
            "宿主机未安装 CANN Toolkit（容器方式部署无需宿主机 CANN，可忽略）"))
    else:
        items.append(HostEnvItem(
            "cann", "CANN 版本", "SKIP", "非 Ascend 服务器，无需 CANN"))

    # ---- 内存大小
    mem_gb = 0
    res = ssh.exec(
        "free -g 2>/dev/null | awk 'NR==2{print $2}'", timeout=15)
    if res.ok:
        try:
            mem_gb = int((res.stdout or "").strip() or 0)
        except ValueError:
            mem_gb = 0
    if mem_gb > 0:
        ok = mem_gb >= _MEM_MIN_GB
        items.append(HostEnvItem(
            "memory", "内存大小",
            "PASS" if ok else "WARN",
            f"{mem_gb} GB",
            "" if ok else
            f"内存 {mem_gb} GB 低于建议值 {_MEM_MIN_GB} GB"
            "（大模型推理建议更大内存）。"))
    else:
        items.append(HostEnvItem(
            "memory", "内存大小", "SKIP", "无法获取（free 不可用）"))

    # ---- Docker 版本（容器创建/批量部署的基础依赖）
    res = ssh.exec("docker --version 2>/dev/null", timeout=15)
    docker_match = (re.search(r"Docker version (\d+)\.(\d+)", res.stdout or "")
                    if res.ok else None)
    if docker_match is not None:
        dv = f"{docker_match.group(1)}.{docker_match.group(2)}"
        ok = ((int(docker_match.group(1)), int(docker_match.group(2)))
              >= _DOCKER_MIN)
        items.append(HostEnvItem(
            "docker", "Docker 版本",
            "PASS" if ok else "WARN",
            f"Docker {dv}",
            "" if ok else
            f"Docker {dv} 低于建议版本 "
            f"{_DOCKER_MIN[0]}.{_DOCKER_MIN[1]}，建议升级。"))
    else:
        items.append(HostEnvItem(
            "docker", "Docker 版本", "FAIL",
            "未检测到 docker 命令",
            "安装 Docker 后重试（容器创建与批量部署均依赖 Docker）。"))

    # ---- 共享内存（/dev/shm 大小，vLLM 需大 shm；df -h Size 列如 512G/1.0T）
    def _size_to_gb(text: str) -> float:
        """df -h 尺寸（如 512G / 1.0T / 256M / 1024K）换算为 GB。"""
        match = re.match(r"([\d.]+)\s*([KMGTPE]?)(i?B?)$", text.strip(),
                         re.IGNORECASE)
        if match is None:
            return 0.0
        value = float(match.group(1))
        unit = (match.group(2) or "B").upper()
        factor = {"B": 1.0 / (1024 ** 3), "K": 1.0 / (1024 ** 2),
                  "M": 1.0 / 1024, "G": 1.0, "T": 1024.0,
                  "P": 1024.0 * 1024, "E": 1024.0 * 1024 * 1024}[unit]
        return value * factor

    res = ssh.exec(
        "df -h /dev/shm 2>/dev/null | awk 'NR==2{print $2}'", timeout=15)
    shm_text = (res.stdout or "").strip() if res.ok else ""
    shm_gb = _size_to_gb(shm_text) if shm_text else 0.0
    if shm_gb > 0:
        ok = shm_gb >= _SHM_MIN_GB
        summary = (f"{shm_gb:.0f} GB" if shm_gb >= 1 else f"{shm_gb * 1024:.0f} MB")
        items.append(HostEnvItem(
            "shm", "共享内存（/dev/shm）",
            "PASS" if ok else "FAIL",
            summary,
            "" if ok else
            f"共享内存 {summary} 低于要求值 {_SHM_MIN_GB} GB；"
            "请在宿主机扩容（如重新挂载：mount -o remount,size=512G /dev/shm）"
            "后重试（大模型推理依赖大共享内存）。"))
    else:
        items.append(HostEnvItem(
            "shm", "共享内存（/dev/shm）", "SKIP",
            "无法获取（df /dev/shm 失败）"))

    # ---- 可用 AIO 资源（须 ≥ 推理卡数 × 4096）
    res = ssh.exec(
        "echo $(($(cat /proc/sys/fs/aio-max-nr) - $(cat /proc/sys/fs/aio-nr)))",
        timeout=15)
    try:
        aio_avail = int((res.stdout or "").strip()) if res.ok else 0
    except ValueError:
        aio_avail = 0
    cards = card_count if card_count and card_count > 0 else 0
    if aio_avail > 0 or res.ok:
        if cards > 0:
            need = cards * _AIO_PER_CARD
            ok = aio_avail >= need
            items.append(HostEnvItem(
                "aio", "AIO 可用资源",
                "PASS" if ok else "WARN",
                f"{aio_avail}（需 ≥ {need}，{cards} 卡 × {_AIO_PER_CARD}）",
                "" if ok else
                f"可用 AIO 资源 {aio_avail} 低于需求 {need}"
                f"（{cards} 张推理卡 × {_AIO_PER_CARD}）；"
                "请调大内核 AIO 上限（如 sysctl -w fs.aio-max-nr=1048576，"
                "并写入 /etc/sysctl.conf 持久化）。"))
        else:
            items.append(HostEnvItem(
                "aio", "AIO 可用资源", "SKIP",
                "未探测到推理卡数（AIO 需求按卡数计算）"))
    else:
        items.append(HostEnvItem(
            "aio", "AIO 可用资源", "SKIP",
            "无法获取（aio-max-nr / aio-nr 读取失败）"))
    return items


def summarize_env_status(items: List["HostEnvItem"]) -> str:
    """汇总环境检测结果：FAIL > WARN > PASS；无数据/无结论返回 ''。"""
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
