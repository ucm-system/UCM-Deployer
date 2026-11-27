# UCM Deployer

**UCM (Unified Cache Management) 安装与环境预检工具 —— Windows 桌面版**

面向 vLLM / vLLM-Ascend / SGLang 运行环境（Ascend NPU / NVIDIA GPU），把 UCM 的安装、环境复检与推理服务部署简化为向导步骤：

```
① 服务器管理 → ② UCM镜像准备 → ③ UCM容器部署 → ④ UCM容器环境复检 → ⑤ 推理服务部署（另有（可选）批量部署面向多台服务器）
```

[快速开始 :material-rocket-launch:](guide/install.md){ .md-button .md-button--primary }
[下载最新版 exe](https://github.com/ucm-system/UCM-Deployer/releases/latest/download/UCM-Deployer.exe){ .md-button }

---

## 功能总览

![步骤1 界面](screenshots/step1-servers.png)

| 步骤 | 功能亮点 |
|---|---|
| **① 服务器管理** | SSH 登录信息本地加密保存/复用；单选登录；`npu-smi`/`nvidia-smi` 设备探测（型号/卡数/CPU 架构），并同步**环境检测**（加速器驱动 / 内核 ≥ 5.10 / **SMMU** / **CANN ≥ 8.0** / **内存 ≥ 32GB** / **Docker ≥ 20.10** / **共享内存 ≥ 512GB** / **可用 AIO ≥ 卡数 × 4096**，与步骤4 同一套判定口径）；结果汇总显示在**「检测结果」列**（悬浮看明细）；另有**批量测试连接 / 批量环境检测**按钮作用于全部服务器 |
| **② UCM镜像准备** | 单一来源选择器：复用服务器已有镜像、离线导入 tar 包（「服务器保存路径」可配置，大镜像包可用大容量目录），或让目标服务器直连 `docker pull`；自动探测基础镜像运行时并校验 wrapt/UCM/ucm-toolkit whl |
| **③ UCM容器部署** | 选 UCM 镜像（**选择时自动校验**）；自动探测并映射实际存在的 Ascend 设备节点、驱动与工具挂载（950 机型自动只读挂载 `/usr/bin/urma_admin`），兼容 Ascend950PR/A5；固定 `--entrypoint /bin/bash` 只创建容器；**统一挂载表**（KV Cache / 模型 / 自定义，一套按钮，**默认表为空**）；容器参数与 docker run 命令**同模块配置、可编辑后执行**；**同名容器自动检测**（存在时明确提示）；**「UCM 文件配置」模块**（配置可编辑 + 容器内路径 + 一键写入容器，写入后步骤5 拉起命令自动联动） |
| **④ UCM容器环境复检** | 容器内运行 `ucm-toolkit run precheck`（环境检查 + **可选带宽测试** + **SMMU 检查**）；镜像未安装 toolkit 时**明确报错**（不伪装通过）；UCM 与驱动版本支持多来源补齐；**预检参数可配置**；结果表格+详情区展示（中文名与排查建议） |
| **⑤ 推理服务部署** | 推理服务拉起命令模板与注意事项（`ENABLE_UCM_PATCH=1` / `VLLM_CPU_AFFINITY=1` 环境变量 + `--kv-transfer-config` 接入 UCMConnector；**需修改片段黄底高亮**，`UCM_CONFIG_FILE` 与步骤3 写入路径自动联动）；**按输入的服务器 IP/端口与模型名自动生成** curl 验证命令（`/health`、`/v1/models`、`/v1/chat/completions`），并可**通过 SSH 在目标服务器上自动执行**连通性测试 |
| **（可选）批量部署** | 多台服务器场景：一台服务器制作镜像后向**多台目标**批量执行「镜像分发 → 容器创建（+UCM 配置写入）→ 预检」；目标列表来自步骤1，**「环境」列显示宿主机环境检测汇总**（PASS/WARN/FAIL/未检测）；tar 中转 / 仓库拉取 / **服务器直传**（镜像不经本机）三种分发方式，分发前总是自动检测目标已有同名镜像并跳过（重复执行安全）；**部署配置四页签**（镜像与分发 / 容器配置 / UCM文件配置 / 预检配置），页签栏右上角三个**执行阶段开关**（取消勾选则对应页签置灰并跳过该阶段）；目标列表**点击行任意位置即可勾选**、点「部署」列头全选/清空、右键菜单刷新；每台独立进度与日志；**部署可随时取消**（远端长任务同步中断）；容器创建后确认运行状态（启动即退出立即诊断）；汇总简短标注结果、完整失败原因见日志页签；**支持自定义 docker run 命令**（同构集群整条执行）；**预检 JSON 预填常用模板**（叠加于出厂默认），**带宽检查与 KV Cache 挂载联动**（无 KV Cache 目录时禁用；挂载目录自动带出）；**部署数据模板（JSON）导出/导入**复用登录信息与容器/UCM文件配置/预检配置 |
| **工具菜单** | 清理指定容器/镜像（关键字筛选、悬浮看全名） |

## 核心特性

- **单服务器流程**：一次登录一台服务器，全部操作作用于勾选的服务器
- **推理服务部署**：UCM 配置写入目标容器（步骤3）+ 拉起命令指引 + 连通性自动测试
- **多服务器批量部署（可选）**：勾选多台目标并行执行镜像分发、容器创建与环境复检，逐台展示进展
- **先展示、可编辑、再执行**：所有生成的命令完整展示给用户，可修改后再执行
- **自由跳转**：步骤间可任意跳转，前置条件在操作执行时拦截提示
- **凭据安全**：密码 Fernet 加密保存在本地（`~/.ucm_deployer/`）

## 环境要求

- 本地：Windows + 双击 `UCM-Deployer.exe` 即可（无需 Python，也无需 Docker Desktop）
- 目标服务器：Linux + SSH + Docker；Ascend 需 `npu-smi`，NVIDIA 需 `nvidia-smi`

## 相关链接

- [GitHub 仓库](https://github.com/ucm-system/UCM-Deployer)
- [下载发布版](https://github.com/ucm-system/UCM-Deployer/releases)
- [UCM 项目（unified-cache-management）](https://github.com/ModelEngine-Group/unified-cache-management)
