# UCM Deployer

**UCM (Unified Cache Management) 安装、环境检测与推理服务拉起工具 —— Windows 桌面版**

:book: **[在线使用文档（含界面截图）](https://ucm-system.github.io/UCM-Deployer/)**

面向 vLLM / vLLM-Ascend / SGLang 运行环境（Ascend NPU / NVIDIA GPU），把 UCM 安装、环境检测与推理服务拉起简化为向导步骤：

```
① 服务器管理（环境检测） → ② UCM镜像准备 → ③ UCM容器环境准备 → ④ UCM容器环境预检 → ⑤ 推理服务拉起
（步骤4 与步骤5 之间另有（可选）批量部署，面向多台服务器）
```

- **步骤1 服务器管理**：SSH 登录信息本地加密保存/复用；单选登录一台服务器，`npu-smi`/`nvidia-smi` 设备探测（型号/卡数/CPU 架构）并同步**宿主机环境检测**（加速器驱动 HDK > 25.2.0 / 内核 ≥ 5.10 / **SMMU** 关闭 / **CANN 版本 ≥ 8.0**（容器方式部署宿主机无 CANN 自动跳过）/ **内存 ≥ 32GB** / **Docker ≥ 20.10**，与步骤4 同一套判定口径，提前暴露基础环境问题）；多台服务器可用**「🧪 批量测试连接」与「🔍 批量环境检测」**并行执行（每台独立日志页签）；列表新增**「检测结果」列**（PASS 绿 / WARN 橙 / FAIL 红 / 未检测灰，悬浮看明细）
- **步骤2 UCM镜像准备**：通过单一“镜像来源”选择器复用服务器已有镜像、离线导入本地 tar 包（「服务器保存路径」可配置，大镜像包可改大容量目录如 `/data`，避免 `/tmp` 空间不足），或输入镜像仓库地址（如 `quay.io/ascend/vllm-ascend:v0.30.0rc2-a5-openeuler`）由**目标服务器**直接 `docker pull`（本地无需 Docker Desktop）；已含 UCM 的镜像可直接复用并跳过构建；确需构建时上传 wrapt/UCM/ucm-toolkit whl，构建前自动校验设备平台与架构
- **步骤3 UCM容器环境准备**：选 UCM 镜像（**选择时自动校验**镜像内 UCM）；自动探测宿主机实际存在的 Ascend 设备节点与驱动/工具挂载，兼容 Ascend 950PR/A5（缺失 `/dev/devmm_svm` 时不再生成该参数，存在 `/dev/ummu` 或 `/dev/uburma` 时自动添加 `--privileged=true`，宿主机存在 `/usr/bin/urma_admin` 时自动**只读挂载**）；GPU 使用 `--runtime nvidia --gpus all -p 8000:8000`；**统一挂载表**（KV Cache 行同路径锁定 / 模型行 / 自定义挂载，**默认表为空**，选中行可校验是否位于同一文件系统）；容器参数（容器名/共享内存/网络）与 docker run 命令**同模块配置、可编辑后执行**；**同名容器自动检测**（存在时明确提示，可经工具菜单清理）；已有 UCM 容器可跳过创；**UCM 文件配置**模块：UCM 配置文件（`UcmPipelineStore` / `Cache|Posix` 存储，**可编辑默认模板**+「🔁 恢复默认模板」）一键**写入目标容器**（SFTP 上传宿主机临时文件 → `docker exec mkdir -p` → `docker cp` → 校验文件开头），写入路径与步骤 5 拉起命令的 `UCM_CONFIG_FILE` 联动一；挂载配置「🌐 新增远端挂载」：宿主机挂载 NFS over RDMA / DTFS 远端文件系统（命令实时预览，成功后自动加入映射表）
- **步骤4 UCM容器环境预检**：**显式选择目标容器**（刷新 `docker ps -a` 后选择已有容器或输入容器名）；在所选容器内运行 `ucm-toolkit run precheck`（驱动/内核/共享内存/AIO/**SMMU** 检查 + **可选挂载目录带宽测试**）；镜像未安装 ucm-toolkit 或预检未产出检查项时**明确报错并给出排查指引**（不伪装成通过）；当 toolkit 无法识别 UCM 平台变体或 Ascend 驱动版本时，自动用 pip 元数据、`version.info`、`npu-smi`、`nvidia-smi` 多来源补齐并标注来源；**预检参数可配置**；结果**表格+详情区展示**（PASS/WARN/FAIL/INFO 着色、中文名与排查建议）
- **步骤5 推理服务拉起**：**拉起命令指引 + 自动验证**（两模块，阅读顺序=操作顺序）——① 推理服务拉起命令：容器内执行的命令模板与注意事项（导出 `ENABLE_UCM_PATCH=1`、`VLLM_CPU_AFFINITY=1`，附加 `--kv-transfer-config` 接入 UCMConnector，需修改片段黄底高亮；PD 分离部署时该参数只需加在 **Prefill 实例**，Decode 实例无需添加）；② 验证连通性：**按输入的服务器 IP（默认 127.0.0.1）、端口（默认 7800）与模型名自动生成** curl 命令（`/health`、`/v1/models`、`/v1/chat/completions` 完整示例，可复制手工执行），或点「⚡ 执行连通性测试」由工具通过 SSH **自动执行并逐项判定**（结果写入任务日志，失败给排查提示）；UCM 配置文件的写入已前移到步骤 3 / 批量部署，本页不再重复
- **（可选）批量部署**：面向**多台服务器**场景——在一台服务器制作好镜像后，勾选多台目标批量执行「镜像分发 → 容器创建 → 环境复检」；镜像分发支持三种方式：**tar 包中转**（源服务器 `docker save -o` 导出 → 本机中转 → 各目标 `docker load`）、**镜像仓库拉取**、**服务器直传**（源服务器 `docker save -o` 导出 → 各目标经服务器间 scp 拉取 → `docker load`，镜像不经本机、不占本机磁盘与带宽；密码认证优先 sshpass，未安装时自动改用 OpenSSH `SSH_ASKPASS` 兜底，日志不回显密码），目标/源端镜像包保存路径可配置且用后自动清理，分发前**总是自动检测**目标已有同名镜像并跳过（重复执行安全）；**部署配置四页签**（镜像与分发 / 容器配置 / **UCM文件配置**（可编辑配置模板 + 容器内路径，随部署模板导出/导入）——是否写入由右上角**「创建容器并配置UCM文件」**阶段开关决定（勾选 = 创建容器并在每台创建完成后写入配置，内容/路径全目标统一；未勾选时容器配置与 UCM文件配置两页签均不可选中）/ 预检配置），页签栏右上角为三个**执行阶段开关**（分发镜像 / 创建容器 / 环境预检，取消勾选则跳过该阶段；镜像与分发页签保持可编辑，不分发镜像时直接填写目标上已有的镜像名）；**预检 JSON 预填常用配置模板**（内核版本 / 带宽参数，叠加于出厂默认之上），**「执行存储带宽检查」与 KV Cache 挂载联动**（容器配置未添加 KV Cache 目录时禁用并提示；挂载目录自动带出第一个 KV Cache 路径，手动编辑后停止带出）；支持**自定义 docker run 命令**（同构集群整条执行、`{image}` 占位符自动替换镜像、可一键生成样例，异构集群仍走逐台自动适配，与附加参数互斥）；目标服务器列表来自步骤1「服务器管理」——**点击行任意位置即可勾选**（勾选行整行高亮）、点「部署」列表头全选/清空、右键菜单可刷新列表，新增**「环境」列**显示步骤1 宿主机环境预检的汇总状态（PASS 绿 / WARN 橙 / FAIL 红 / 未探测灰，悬浮看各项明细）；**部署可随时取消**（远端长任务同步中断），容器创建后自动确认运行状态（启动即退出时立即诊断并提示 `docker logs` 定位），汇总简短标注结果、完整失败原因见各服务器日志页签；每台服务器独立进度条 + 独立日志页签，按设备类型（Ascend/NVIDIA）自动适配容器参数；**部署模板（JSON）导出/导入**：整套批量部署方案（服务器登录信息——按主机+端口+用户名匹配写入服务器管理并自动勾选为目标、镜像与分发、容器/预检配置、执行阶段开关）一次导出，其他电脑导入即用；模板为纯 JSON 可手工编；挂载表右键菜单「移除选中 / 批量校验」，「🌐 批量新增远端挂载」在全部勾选目标宿主机上挂载 NFS over RDMA / DTFS（成功后加入映射表）
- **工具菜单**：清理指定容器（`docker ps -a` 勾选删除）/ 清理指定镜像（`docker images` 勾选删除），作用于当前登录的服务器
- 步骤间**自由跳转**（前置条件在操作执行时拦截）；**切换服务器自动清理**上一台残留的镜像列表、设备探测与 docker run 命令（避免误按旧命令操作新服务器）；所有生成的命令**先展示、可编辑、再执行**；全部步骤页面在窗口过小时**内部滚动**，窗口可自由缩放

## 快速开始

### 直接使用 exe

双击 `UCM-Deployer.exe` 即可（无需 Python）。自检：

```powershell
.\UCM-Deployer.exe --smoke
```

### 源码运行 / 开发

```powershell
python -m venv .venv                       # Python 3.9+
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest             # 全部自检测试
.venv\Scripts\python main.py               # GUI
.venv\Scripts\python main.py --smoke       # 无头 GUI 冒烟
```

### 打包 exe

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# 先跑全部测试，再产出 dist\UCM-Deployer.exe
```

### 云端自动构建发布（推荐）

推送 `v*` 标签即触发 GitHub Actions：云端跑全量测试 → PyInstaller 打包 → 产物自检（`--smoke`）→ 自动挂到对应 Release（`.github/workflows/release.yml`）：

```powershell
git tag v0.2.0 && git push origin v0.2.0
```

也支持在 Actions 页面手动运行（workflow_dispatch，指定标签）。

## 自验证体系（无真实服务器）

| 层级 | 手段 | 覆盖 |
|---|---|---|
| 单元测试 | `tests/`（FakeSSH 脚本化 paramiko 替身） | SSH/加密/注册表/设备与环境检测解析/docker 解析/Dockerfile/命令生成/匹配校验/服务页写入与连通性测试/SMMU/批量部署/部署模板 等 296 项 |
| GUI | offscreen 冒烟 | 各页面实例化、导航守卫、单选/多选、镜像筛选、下载链接、whl 反馈、批量执行、模板导入导出、像素采样断言界面无黑块 |
| 产物 | `exe --smoke` | 打包后程序自检 |

## 文档

- [用户手册](docs/用户手册.md) —— 安装、五步操作详解、FAQ
- [架构设计](docs/architecture.md) —— 模块划分、关键决策

## 目录结构

```
UCM-Deployer/
├── main.py                       # GUI 入口（--smoke 无头自检）
├── ucm_deployer/
│   ├── core/                     # 核心逻辑（无 GUI 依赖）
│   │   ├── models.py             # 数据模型
│   │   ├── crypto.py             # 凭据加密
│   │   ├── ssh_client.py         # SSH/SFTP 封装
│   │   ├── server_registry.py    # 服务器注册表
│   │   ├── device_detector.py    # 设备/架构探测 + 宿主机环境检测
│   │   ├── docker_manager.py     # docker 操作
│   │   ├── image_builder.py      # 匹配校验 + 镜像构建
│   │   ├── container_manager.py  # docker run 生成/执行
│   │   └── batch_deploy.py       # 批量部署 + 部署模板 + 镜像分发
│   ├── gui/                      # PySide6 向导（5 步骤，批量部署位于 4/5 之间）
│   │   ├── widgets/              # 五个步骤页面 + 通用组件
│   │   ├── main_window.py        # 主窗口/导航守卫/状态栏
│   │   └── theme.py              # QSS 主题 + 图标
│   └── utils/                    # 日志/路径
├── tests/                        # 296 项测试（FakeSSH + GUI 冒烟）
├── docs/                         # 用户手册 / 架构文档
├── scripts/build_exe.ps1         # 打包脚本
└── ucm_deployer.spec             # PyInstaller 配置
```

## 依赖

- Python ≥ 3.9（开发/源码运行）
- paramiko（SSH/SFTP）、PySide6（GUI）、cryptography（凭据加密）
- 目标服务器：Linux + SSH + Docker；Ascend 需 npu-smi，NVIDIA 需 nvidia-smi
