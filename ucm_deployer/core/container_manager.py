# -*- coding: utf-8 -*-
"""容器创建：Ascend/NVIDIA 的 docker run 命令生成与执行、
kvcache 挂载目录文件系统校验、宿主机目录浏览。"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .docker_manager import DockerError, DockerManager
from .models import CommandResult, DeviceType, VolumeMount
from .shell import flatten_command, shq
from .ssh_client import SSHClient

# Ascend 容器候选系统挂载（按宿主机实际存在情况筛选；父目录存在时跳过子路径）。
# 注意：不要挂载 /usr/lib64 —— 宿主机动态库覆盖容器内库目录会导致容器
# 内程序（含 bash）加载失败并立即退出。
ASCEND_SYSTEM_MOUNT_CANDIDATES: List[str] = [
    "/usr/local/Ascend/driver",
    "/usr/local/Ascend/driver/lib64",
    "/usr/local/Ascend/driver/tools/hccn_tool",
    "/usr/local/Ascend/driver/version.info",
    "/usr/local/dcmi",
    "/usr/local/sbin",
    "/usr/local/bin/npu-smi",
    "/usr/bin/urma_admin",
    "/etc/ascend_install.info",
    "/etc/hccl_rootinfo.json",
    "/etc/hixlep",
    "/root/.cache",
]

# 950（A5）机型的 URMA 管理工具：宿主机实际存在时以只读方式挂载
ASCEND_SYSTEM_MOUNTS_READONLY = {"/usr/bin/urma_admin"}

ASCEND_SYSTEM_MOUNTS: List[Tuple[str, str]] = [
    (path, path) for path in ASCEND_SYSTEM_MOUNT_CANDIDATES
]

# 视为可跨服务器共享的网络文件系统
NETWORK_FS_TYPES = {
    "nfs", "nfs4", "3fs", "beegfs", "lustre", "ceph", "cephfs",
    "glusterfs", "gpfs", "cifs", "smbfs", "juicefs", "gds",
}


@dataclass
class ContainerCreateConfig:
    image: str
    name: str
    device_type: DeviceType = DeviceType.UNKNOWN
    device_count: int = 0
    shm_size: str = "512g"
    network: str = "host"                 # Ascend 使用（--net=host）
    ports: List[str] = field(default_factory=lambda: ["8000:8000"])  # NVIDIA 使用（-p）
    kv_cache_dirs: List[str] = field(default_factory=list)
    model_mount: Optional[VolumeMount] = None
    extra_mounts: List[VolumeMount] = field(default_factory=list)
    extra_args: str = ""
    device_nodes: List[str] = field(default_factory=list)
    system_mounts: List[Tuple[str, str]] = field(default_factory=list)
    privileged: bool = False

    def validate(self) -> None:
        if not self.image:
            raise DockerError("未选择镜像")
        if not self.name or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", self.name):
            raise DockerError(f"非法的容器名: {self.name!r}")
        for d in self.kv_cache_dirs:
            if not d.startswith("/"):
                raise DockerError(f"kvcache 挂载目录必须是绝对路径: {d}")
        for node in self.device_nodes:
            if not node.startswith("/dev/") or re.search(r"\s", node):
                raise DockerError(f"非法的 Ascend 设备节点: {node!r}")
        for host, container in self.system_mounts:
            if not host.startswith("/") or not container.startswith("/"):
                raise DockerError(f"非法的 Ascend 系统挂载: {host!r} -> {container!r}")


@dataclass
class FsEntry:
    path: str
    source: str = ""
    fstype: str = ""
    error: str = ""


@dataclass
class SharedFsReport:
    entries: List[FsEntry] = field(default_factory=list)
    ok: bool = False
    message: str = ""
    warnings: List[str] = field(default_factory=list)


def _is_network_fs(entry: FsEntry) -> bool:
    return entry.fstype.lower() in NETWORK_FS_TYPES or ":" in entry.source


def evaluate_shared_fs(entries: List[FsEntry]) -> SharedFsReport:
    """评估 kvcache 挂载目录的文件系统（单服务器）。"""
    report = SharedFsReport(entries=entries)
    if not entries:
        report.ok = False
        report.message = "没有待校验的目录"
        return report

    errors = [e for e in entries if e.error]
    if errors:
        report.ok = False
        report.message = "；".join(f"{e.path} {e.error}" for e in errors)
        return report

    sources = {e.source for e in entries}
    if len(sources) > 1:
        report.ok = False
        report.message = ("kvcache 目录不在同一文件系统: "
                          + ", ".join(f"{e.path}({e.source})" for e in entries))
        return report

    sample = entries[0]
    if _is_network_fs(sample):
        report.ok = True
        report.message = f"校验通过：目录位于网络文件系统({sample.source}, {sample.fstype})"
    else:
        report.ok = True
        report.message = f"目录位于本地文件系统({sample.source}, {sample.fstype})"
        report.warnings.append(
            "当前为本地磁盘；如后续需共用 kvcache，建议改用共享文件系统(NFS/3FS等)")
    return report


def check_shared_fs(ssh: SSHClient, dirs: List[str]) -> SharedFsReport:
    """df 校验 kvcache 挂载目录所在文件系统。"""
    entries: List[FsEntry] = []
    for d in dirs:
        res = ssh.exec(f"df --output=source,fstype {shq(d)} 2>/dev/null", timeout=30)
        lines = [l for l in res.stdout.strip().splitlines() if l.strip()]
        if not res.ok or len(lines) < 2:
            entries.append(FsEntry(d, error="目录不存在或无法读取"))
            continue
        parts = lines[-1].split()
        entries.append(FsEntry(d,
                               source=parts[0] if parts else "",
                               fstype=parts[1] if len(parts) > 1 else ""))
    return evaluate_shared_fs(entries)


class ContainerManager:
    def __init__(self, ssh: SSHClient, docker: Optional[DockerManager] = None):
        self.ssh = ssh
        self.docker = docker or DockerManager(ssh)

    # ------------------------------------------------------------ 命令生成
    @staticmethod
    def generate_run_command(cfg: ContainerCreateConfig) -> str:
        cfg.validate()
        lines = ["docker run -itd"]
        lines.append(f"    --name {shq(cfg.name)}")
        lines.append("    --entrypoint /bin/bash")

        if cfg.device_type == DeviceType.ASCEND:
            # Ascend: --net=host + 全部 davinci 设备 + 驱动/工具挂载
            if cfg.network:
                lines.append(f"    --net={cfg.network}")
            if cfg.privileged:
                lines.append("    --privileged=true")
            if cfg.shm_size:
                lines.append(f"    --shm-size {cfg.shm_size}")
            device_nodes = cfg.device_nodes or (
                [f"/dev/davinci{i}" for i in range(cfg.device_count)]
                + ["/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc"])
            for node in device_nodes:
                lines.append(f"    --device {shq(node)}")
            system_mounts = cfg.system_mounts or ASCEND_SYSTEM_MOUNTS
            for host, cont in system_mounts:
                suffix = ":ro" if host in ASCEND_SYSTEM_MOUNTS_READONLY else ""
                lines.append(f"    -v {shq(host)}:{shq(cont)}{suffix}")
        elif cfg.device_type == DeviceType.NVIDIA:
            # NVIDIA: --runtime nvidia --gpus all + 端口映射 + --ipc=host
            lines.append("    --runtime nvidia")
            lines.append("    --gpus all")
            for p in cfg.ports:
                if p.strip():
                    lines.append(f"    -p {p.strip()}")
            lines.append("    --ipc=host")
            if cfg.shm_size:
                lines.append(f"    --shm-size {cfg.shm_size}")
        else:
            # 设备未知：保守输出通用参数
            if cfg.network:
                lines.append(f"    --net={cfg.network}")
            if cfg.shm_size:
                lines.append(f"    --shm-size {cfg.shm_size}")

        for d in cfg.kv_cache_dirs:
            d = d.rstrip("/") or "/"
            lines.append(f"    -v {shq(d)}:{shq(d)}")
        if cfg.model_mount is not None:
            lines.append("    " + cfg.model_mount.to_docker_arg())
        for m in cfg.extra_mounts:
            lines.append("    " + m.to_docker_arg())
        for extra in cfg.extra_args.splitlines():
            if extra.strip():
                lines.append("    " + extra.strip())
        lines.append(f"    {shq(cfg.image)}")
        return " \\\n".join(lines)

    def list_ascend_devices(self) -> List[str]:
        """列出宿主机实际存在的 Ascend 设备节点（不同型号驱动节点不同）。"""
        cmd = (
            "for d in /dev/davinci* /dev/devmm_svm /dev/hisi_hdc "
            "/dev/ummu /dev/uburma; do "
            "if [ -e \"$d\" ]; then printf '%s\\n' \"$d\"; fi; done | sort -u -V"
        )
        res = self.ssh.exec(cmd, timeout=30)
        if not res.ok:
            raise DockerError(
                f"探测 Ascend 设备节点失败: {res.stderr.strip()[:300]}")
        nodes = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if not nodes:
            raise DockerError("未找到 /dev/davinci* 等 Ascend 设备节点")
        return nodes

    def list_ascend_mounts(self) -> List[Tuple[str, str]]:
        """列出宿主机实际存在的 Ascend 驱动/工具/配置挂载路径。"""
        candidates = " ".join(shq(path) for path in ASCEND_SYSTEM_MOUNT_CANDIDATES)
        cmd = (
            f"for p in {candidates}; do "
            "if [ -e \"$p\" ]; then printf '%s\\n' \"$p\"; fi; done"
        )
        res = self.ssh.exec(cmd, timeout=30)
        if not res.ok:
            raise DockerError(
                f"探测 Ascend 系统挂载失败: {res.stderr.strip()[:300]}")
        selected: List[Tuple[str, str]] = []
        for host in (line.strip().rstrip("/") for line in res.stdout.splitlines()
                     if line.strip()):
            # 只接受候选列表内的路径（候选已排除 /usr/lib64 等会破坏
            # 容器内动态库的挂载）
            if host not in ASCEND_SYSTEM_MOUNT_CANDIDATES:
                continue
            nested = any(
                host == parent or host.startswith(parent + "/")
                for parent, _ in selected
            )
            if not nested:
                selected.append((host, host))
        return selected

    # ------------------------------------------------------------ 执行
    @staticmethod
    def _validate_shell_entrypoint(command: str) -> None:
        """确保容器只启动 Bash，不触发镜像默认的推理服务入口。"""
        flat = flatten_command(command)
        try:
            tokens = shlex.split(flat)
        except ValueError as exc:
            raise DockerError(f"docker run 命令引号不合法: {exc}") from exc

        entrypoint = ""
        for index, token in enumerate(tokens):
            if token == "--entrypoint":
                if index + 1 >= len(tokens):
                    break
                entrypoint = tokens[index + 1]
            elif token.startswith("--entrypoint="):
                entrypoint = token.split("=", 1)[1]

        if entrypoint != "/bin/bash":
            raise DockerError(
                "创建命令必须包含 --entrypoint /bin/bash，"
                "以覆盖镜像默认入口并避免启动 vLLM 推理服务")
        if tokens and tokens[-1] in ("bash", "/bin/bash"):
            raise DockerError(
                "创建命令不得在镜像后追加 bash；"
                "--entrypoint /bin/bash 已保证容器进入 Bash 并保持运行")

    def create(self, command: str, on_line=None,
               cancelled=None) -> CommandResult:
        """执行（可能被用户编辑过的）docker run 命令。

        退出码非零时抛 DockerError（含 stderr 摘要）——否则容器名冲突等
        失败会被误判为成功，批量流水线会继续执行后续阶段。
        支持直接粘贴日志中的命令行：容忍行尾「\\ 空白」续行与「$ 」前缀。
        """
        self._validate_shell_entrypoint(command)
        flat = flatten_command(command)
        if flat.startswith("$ "):
            flat = flat[2:].strip()      # 日志前缀 "$ docker run ..." -> 去掉
        res = self.docker.run(flat, on_line=on_line, cancelled=cancelled)
        if not res.ok:
            detail = (res.stderr or res.stdout or "").strip()
            raise DockerError(
                f"docker run 失败(退出码 {res.exit_code}): {detail[:400]}")
        return res

    # ------------------------------------------------------------ 浏览
    def list_host_dirs(self, path: str = "/") -> List[str]:
        """列出宿主机某目录下的子目录（GUI 目录浏览数据源）。"""
        path = path.rstrip("/") or "/"
        res = self.ssh.exec(
            f"find {shq(path)} -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort | head -200",
            timeout=30)
        if not res.ok:
            return []
        return [l.strip() for l in res.stdout.splitlines() if l.strip()]


def ensure_container_removed(ssh: SSHClient, docker: DockerManager,
                             name: str, recreate: bool,
                             log: Optional[Callable[[str], None]] = None,
                             cancelled: Optional[Callable[[], bool]] = None
                             ) -> None:
    """创建容器前处理同名容器（步骤3 与批量部署共用）。

    - 不存在同名容器：直接返回；
    - 存在且未勾选重建：抛 DockerError 明确提示处理方式；
    - 存在且勾选重建：docker rm -f 删除并验证（rm 失败且容器仍在时报错，
      杜绝静默失败后 docker run 才暴露 Conflict）。
    """
    log = log or (lambda line: None)

    def _names() -> set:
        return {c["names"] for c in docker.list_containers(all_states=True)}

    if name not in _names():
        return
    if not recreate:
        raise DockerError(
            f"已存在同名容器 {name}：请勾选「同名容器先删除再创建」后重试，"
            "或先手动删除旧容器")
    log(f"$ docker rm -f {name}（已选择重建容器）")
    res = ssh.exec_stream(f"docker rm -f {shq(name)}", timeout=120,
                          cancelled=cancelled)
    combined = ((res.stderr or "") + (res.stdout or "")).lower()
    if not res.ok and "no such container" not in combined:
        # rm 退出码非零且不是「容器不存在」：再确认一次，仍存在则报错
        if name in _names():
            detail = (res.stderr or res.stdout or "").strip()[:200]
            raise DockerError(
                f"删除旧容器 {name} 失败，请手动删除后重试"
                + (f"（{detail}）" if detail else ""))


class UcmConfigError(Exception):
    """UCM 配置文件写入容器失败。"""


# UCM 参数配置文件参考默认模板（参数请按实际存储环境修改）
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

# UCM 配置文件在容器内的默认保存路径（与拉起命令模板保持一致）
UCM_CONFIG_DEFAULT_PATH = "/workspace/ucm_config_example.yaml"


def write_ucm_config_file(ssh: SSHClient, container: str, path: str,
                          content: str,
                          log: Optional[Callable[[str], None]] = None,
                          cancelled: Optional[Callable[[], bool]] = None,
                          tmp_dir: str = "/tmp") -> None:
    """把 UCM 配置文件（yaml 文本）写入目标容器（步骤3 与批量部署共用）。

    流程：SFTP 上传宿主机临时文件 → 容器内 mkdir -p → docker cp →
    读取文件开头校验 → 删除宿主机临时文件（finally 保证清理）。
    失败抛 UcmConfigError；cancelled 回调在长命令前检查。
    """
    log = log or (lambda line: None)
    container = (container or "").strip()
    path = (path or "").strip()
    if not container:
        raise UcmConfigError("目标容器名为空")
    if not path:
        raise UcmConfigError("容器内配置文件路径为空")
    if not content.strip():
        raise UcmConfigError("配置内容为空")

    import uuid as _uuid

    tmp = f"{tmp_dir.rstrip('/')}/.ucm-cfg-{_uuid.uuid4().hex[:8]}.yaml"
    log(f"写入 UCM 配置文件 → 容器 {container}：{path}")
    try:
        ssh.write_file(tmp, content)
        log(f"已上传宿主机临时文件 {tmp}")
        d = path.rsplit("/", 1)[0] if "/" in path else "."
        res = ssh.exec(f"docker exec {shq(container)} mkdir -p {shq(d)}",
                       timeout=30)
        if not res.ok:
            raise UcmConfigError(
                f"容器内创建目录失败：{(res.stderr or res.stdout).strip()[:200]}")
        res = ssh.exec(
            f"docker cp {shq(tmp)} {shq(container)}:{shq(path)}",
            timeout=60)
        if not res.ok:
            raise UcmConfigError(
                f"docker cp 失败：{(res.stderr or res.stdout).strip()[:200]}")
        log("已拷贝进容器")
        if cancelled is not None and cancelled():
            raise UcmConfigError("已取消")
        check = ssh.exec(
            f"docker exec {shq(container)} head -5 {shq(path)}",
            timeout=30)
        if check.ok:
            log("容器内文件开头：\n" + (check.stdout or "").rstrip())
        else:
            raise UcmConfigError(
                "写入后校验失败（容器内读取文件出错）："
                f"{(check.stderr or check.stdout).strip()[:200]}")
    finally:
        ssh.exec(f"rm -f {shq(tmp)}", timeout=30)
