# -*- coding: utf-8 -*-
"""GUI 全局状态（单服务器流程：一次只登录一台服务器）。"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..core.models import DeviceInfo, ServerInfo
from ..core.server_registry import ServerRegistry


class AppContext:
    """三个步骤页面共享的状态。"""

    def __init__(self, registry: Optional[ServerRegistry] = None):
        self.registry = registry or ServerRegistry()
        # 步骤1：当前登录的服务器（至多一台）
        self.selected: List[ServerInfo] = []
        self.devices: Dict[str, DeviceInfo] = {}      # server_id -> DeviceInfo
        # 步骤1 环境检测时的宿主机检查结果（server_id -> List[HostEnvItem]），
        # 供服务器页「检测结果」列与批量页目标表展示可用状态
        self.env_results: Dict[str, list] = {}
        # 步骤2：构建完成/选定的 UCM 镜像
        self.images: Dict[str, str] = {}              # server_id -> 镜像 ref
        # 步骤3：创建的容器名
        self.containers: Dict[str, str] = {}          # server_id -> 容器名
        # 步骤3「UCM文件配置」写入容器后的保存路径
        # （server_id -> 容器内路径；步骤5 拉起命令的 UCM_CONFIG_FILE 按此联动）
        self.ucm_config_paths: Dict[str, str] = {}

    # ------------------------------------------------------------ 辅助
    @property
    def server(self) -> Optional[ServerInfo]:
        """当前登录的服务器（至多一台）。"""
        return self.selected[0] if self.selected else None

    def device_of(self, server: ServerInfo) -> DeviceInfo:
        return self.devices.get(server.id) or (server.device or DeviceInfo())

    def reset_after_servers_changed(self) -> None:
        """登录服务器变化时，后续步骤的依赖状态失效。"""
        ids = {s.id for s in self.selected}
        self.images = {k: v for k, v in self.images.items() if k in ids}
        self.containers = {k: v for k, v in self.containers.items() if k in ids}
        self.ucm_config_paths = {k: v for k, v in self.ucm_config_paths.items()
                                 if k in ids}
