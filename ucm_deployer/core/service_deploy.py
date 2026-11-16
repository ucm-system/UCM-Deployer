# -*- coding: utf-8 -*-
"""服务部署：写入 UCM 配置文件并在目标容器内一键拉起推理服务。

两个输入框之间的衔接方式：
- 「UCM 配置文件内容」在部署时被写入容器内 config_path（默认
  /etc/ucm/config.yaml，可在界面修改）；
- 「推理服务拉起命令」由用户直接引用该路径，推理服务启动时从该
  路径读取配置。二者通过容器内路径解耦，界面无需解析命令内容。

部署流水线（单台服务器、单个容器）：
1. 检查容器存在且运行中；
2. 以 base64 方式写入配置（避免引号/特殊字符转义问题）；
3. 回读字节数校验写入完整性；
4. nohup 后台执行拉起命令，输出重定向到容器内日志文件；
5. 等待 2 秒后回读日志尾部，立即反馈服务启动输出。
"""
from __future__ import annotations

import base64
import posixpath
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List

from .shell import shq
from .ssh_client import SSHClient

# 服务启动输出在容器内的固定位置（每次部署覆盖；服务运行日志也写在这里）
SERVICE_LOG_PATH = "/tmp/ucm-service.log"
SERVICE_PID_PATH = "/tmp/ucm-service.pid"
DEFAULT_CONFIG_PATH = "/etc/ucm/config.yaml"

ProgressFn = Callable[[int, str], None]
LogFn = Callable[[str], None]


class ServiceDeployError(Exception):
    """服务部署配置或执行错误。"""


@dataclass
class ServiceDeployConfig:
    """单机服务部署参数（目标容器 + 配置文件 + 拉起命令）。"""

    container: str
    config_path: str = DEFAULT_CONFIG_PATH
    config_content: str = ""
    start_cmd: str = ""

    def validate(self) -> None:
        if not self.container or not re.match(
                r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", self.container):
            raise ServiceDeployError(f"非法的容器名: {self.container!r}")
        if not self.config_path.startswith("/"):
            raise ServiceDeployError(
                "配置文件路径必须是容器内绝对路径（以 / 开头）")
        if not self.config_content.strip():
            raise ServiceDeployError("UCM 配置文件内容不能为空")
        if not self.start_cmd.strip():
            raise ServiceDeployError("推理服务拉起命令不能为空")


@dataclass
class ServiceDeployReport:
    """部署结果：各阶段完成情况 + 服务启动日志尾部。"""

    ok: bool = False
    container: str = ""
    config_path: str = ""
    config_bytes: int = 0
    service_log_tail: str = ""
    steps: List[str] = field(default_factory=list)


def deploy_service(ssh: SSHClient, cfg: ServiceDeployConfig, *,
                   progress: ProgressFn = lambda p, m: None,
                   log: LogFn = lambda m: None,
                   timeout: float = 120) -> ServiceDeployReport:
    """执行部署流水线，任一阶段失败抛 ServiceDeployError。"""
    cfg.validate()
    rep = ServiceDeployReport(container=cfg.container,
                              config_path=cfg.config_path)

    def _in_ctr(cmd: str, timeout: float = timeout):
        return ssh.exec(
            f"docker exec {shq(cfg.container)} /bin/bash -c {shq(cmd)}",
            timeout=timeout)

    # 1. 容器检查
    progress(5, f"检查容器 {cfg.container} ...")
    r = ssh.exec("docker inspect -f '{{.State.Running}}' "
                 + shq(cfg.container))
    if r.exit_code != 0 or "true" not in (r.stdout or "").lower():
        raise ServiceDeployError(
            f"容器 {cfg.container} 不存在或未运行，请先在步骤3 创建容器")
    rep.steps.append("容器检查")
    log(f"✓ 容器 {cfg.container} 运行中")

    # 2. 写配置（base64：对引号/换行/特殊字符免疫）
    progress(25, f"写入 UCM 配置文件 {cfg.config_path} ...")
    payload = base64.b64encode(cfg.config_content.encode("utf-8")).decode("ascii")
    parent = posixpath.dirname(cfg.config_path) or "/"
    r = _in_ctr(
        f"mkdir -p {shq(parent)} && printf '%s' {shq(payload)}"
        f" | base64 -d > {shq(cfg.config_path)}")
    if r.exit_code != 0:
        raise ServiceDeployError(
            f"写入配置文件失败: {(r.stderr or '').strip() or '未知错误'}")
    rep.steps.append("写入配置")

    # 3. 校验（回读字节数与本地编码后大小一致）
    progress(50, "校验配置文件完整性 ...")
    r = _in_ctr(f"wc -c < {shq(cfg.config_path)}")
    try:
        size = int((r.stdout or "0").strip() or 0)
    except ValueError:
        size = -1
    expect = len(cfg.config_content.encode("utf-8"))
    if r.exit_code != 0 or size != expect:
        raise ServiceDeployError(
            f"配置文件校验失败: 容器内 {size} 字节 != 预期 {expect} 字节")
    rep.config_bytes = size
    rep.steps.append("配置校验")
    log(f"✓ 配置已写入 {cfg.config_path}（{size} 字节）")

    # 4. 后台拉起（nohup + 输出重定向；立即返回不阻塞部署流程）
    progress(70, "拉起推理服务 ...")
    inner = (f"nohup {cfg.start_cmd} > {shq(SERVICE_LOG_PATH)} 2>&1"
             f" & echo $! > {shq(SERVICE_PID_PATH)}")
    r = _in_ctr(inner, timeout=30)
    if r.exit_code != 0:
        raise ServiceDeployError(
            f"拉起命令执行失败: {(r.stderr or '').strip() or '未知错误'}")
    rep.steps.append("拉起服务")
    log(f"✓ 服务已后台启动（容器内日志 {SERVICE_LOG_PATH}）")

    # 5. 启动日志回读（给服务 2 秒初始化时间）
    progress(90, "读取服务启动日志 ...")
    time.sleep(2)
    r = _in_ctr(f"tail -n 100 {shq(SERVICE_LOG_PATH)}", timeout=30)
    rep.service_log_tail = (r.stdout or "")
    if rep.service_log_tail.strip():
        log("---- 服务启动日志（尾部 100 行）----")
        for line in rep.service_log_tail.splitlines():
            log(line)
    progress(100, "部署完成")
    rep.ok = True
    return rep
