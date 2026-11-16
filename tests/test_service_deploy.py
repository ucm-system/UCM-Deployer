# -*- coding: utf-8 -*-
"""服务部署 core 测试：流水线命令序列、配置写入/校验、失败分支。"""
import base64
import re

import pytest

from ucm_deployer.core.models import ServerInfo
from ucm_deployer.core.service_deploy import (
    SERVICE_LOG_PATH,
    ServiceDeployConfig,
    ServiceDeployError,
    deploy_service,
)
from ucm_deployer.core.ssh_client import SSHClient


def make_ssh(fake_ssh) -> SSHClient:
    ssh = SSHClient(ServerInfo.create(name="s", host="127.0.0.1"))
    ssh.connect()
    return ssh


def _script_pipeline(ssh, content: str, service_log: str = "started\n"):
    """注册部署流水线全部命令的 fake 返回（实例级，LIFO 后注册优先）。"""
    ssh._client.add(r"^docker inspect", stdout="true\n")
    ssh._client.add(r"base64 -d", stdout="")               # 写配置（成功）
    ssh._client.add(r"wc -c", stdout=f"{len(content.encode('utf-8'))}\n")
    ssh._client.add(r"nohup", stdout="12345\n")
    ssh._client.add(r"tail -n", stdout=service_log)


def test_deploy_service_success(fake_ssh, monkeypatch):
    monkeypatch.setattr("ucm_deployer.core.service_deploy.time.sleep",
                        lambda s: None)
    ssh = make_ssh(fake_ssh)
    content = "ucm:\n  kv_cache: /mnt/kvcache\n"
    _script_pipeline(ssh, content, service_log="service started\n")

    logs = []
    rep = deploy_service(
        ssh,
        ServiceDeployConfig(
            container="ucm-ctr", config_path="/etc/ucm/config.yaml",
            config_content=content,
            start_cmd="vllm serve /m --ucm-config /etc/ucm/config.yaml"),
        log=logs.append)

    assert rep.ok
    assert rep.container == "ucm-ctr"
    assert rep.config_bytes == len(content.encode("utf-8"))
    assert rep.service_log_tail.strip() == "service started"
    assert rep.steps == ["容器检查", "写入配置", "配置校验", "拉起服务"]
    # 日志含配置写入与启动日志段
    assert any("配置已写入" in m for m in logs)
    assert any("服务启动日志" in m for m in logs)

    cmds = [c.executed_command for c in ssh._client.channels]
    # 1) base64 写入命令：解码后与原文一致
    wr = [c for c in cmds if "base64 -d" in c
          and "/etc/ucm/config.yaml" in c]
    assert len(wr) == 1
    m = re.search(r"printf '\\''%s'\\'' ([A-Za-z0-9+/=]+)", wr[0])
    assert m is not None
    assert base64.b64decode(m.group(1)).decode("utf-8") == content
    # 2) mkdir -p 在写入命令中
    assert "mkdir -p /etc/ucm" in wr[0]
    # 3) nohup 拉起：输出重定向到服务日志
    up = [c for c in cmds if "nohup vllm serve" in c]
    assert len(up) == 1
    assert SERVICE_LOG_PATH in up[0]
    assert "2>&1" in up[0]
    # 4) 回读启动日志
    assert any("tail -n 100" in c for c in cmds)


def test_deploy_service_container_not_running(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^docker inspect", stdout="false\n")
    with pytest.raises(ServiceDeployError, match="不存在或未运行"):
        deploy_service(ssh, ServiceDeployConfig(
            container="ucm-ctr", config_content="x", start_cmd="y"))


def test_deploy_service_write_failure(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^docker inspect", stdout="true\n")
    ssh._client.add(r"base64 -d", stderr="No such file\n", exit_code=1)
    with pytest.raises(ServiceDeployError, match="写入配置文件失败"):
        deploy_service(ssh, ServiceDeployConfig(
            container="ucm-ctr", config_content="x", start_cmd="y"))


def test_deploy_service_size_mismatch(fake_ssh):
    ssh = make_ssh(fake_ssh)
    ssh._client.add(r"^docker inspect", stdout="true\n")
    ssh._client.add(r"base64 -d", stdout="")
    ssh._client.add(r"wc -c", stdout="1\n")
    with pytest.raises(ServiceDeployError, match="校验失败"):
        deploy_service(ssh, ServiceDeployConfig(
            container="ucm-ctr", config_content="hello", start_cmd="y"))


def test_deploy_service_validate():
    with pytest.raises(ServiceDeployError, match="容器名"):
        ServiceDeployConfig(container="", config_content="x",
                            start_cmd="y").validate()
    with pytest.raises(ServiceDeployError, match="绝对路径"):
        ServiceDeployConfig(container="c", config_path="etc/x",
                            config_content="x", start_cmd="y").validate()
    with pytest.raises(ServiceDeployError, match="配置文件内容"):
        ServiceDeployConfig(container="c", config_content="  ",
                            start_cmd="y").validate()
    with pytest.raises(ServiceDeployError, match="拉起命令"):
        ServiceDeployConfig(container="c", config_content="x",
                            start_cmd="").validate()
