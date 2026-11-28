# 步骤3 · UCM容器部署

基于 UCM 镜像创建 UCM 容器：自动探测并映射实际存在的加速卡设备节点与系统资源，配置容器目录映射（支持 NFS over RDMA / DTFS 远端挂载），生成的 docker run 命令**可编辑后执行**。

工具固定生成 `--entrypoint /bin/bash`，且不在镜像后追加 `bash`。容器只创建并保持在 Bash 交互环境中，**不会启动 vLLM 或其他推理服务**。

![步骤3 界面](../screenshots/step3-container.png)

## 操作流程

### 1. 选择 UCM 镜像

进入本页自动加载镜像列表；**选择镜像时自动后台校验镜像内 UCM 安装情况**：

- ✓ 已安装：显示版本，自动记录为本服务器使用的镜像；
- ✗ 未安装：提示回步骤2 构建或换镜像（创建容器前会再次拦截确认）。

### 2. 容器目录映射（统一映射表）

KV Cache / 模型 / 自定义挂载统一在同一张表格中维护（类型、宿主机路径、容器内路径、只读四列）：上方选择类型、填写路径后点「➕ 添加」；**右键点击表格行**可「🗑 移除选中」「🔍 校验选中挂载」；**默认表为空（即不挂载任何目录）**：

- **KV Cache 行**：挂载到容器内相同路径（vLLM 按相同路径读取），「浏览」弹出**容器内/宿主机目录浏览器**；右键「🔍 校验选中挂载」（可 Ctrl 多选）检查挂载点所在文件系统（KV Cache 要求同一文件系统）；
- **模型行**：宿主机路径 → 容器内路径（默认 `/models`），默认只读，最多一行；
- **自定义行**：任意宿主机/容器路径映射，可勾选只读。

![目录浏览](../screenshots/dialog-remote-dirs.png)

#### 新增远端挂载（NFS over RDMA / DTFS）

「➕ 添加」右侧的 **「🌐 新增远端挂载」** 在宿主机上挂载远端文件系统，支持 **NFS over RDMA** 与 **DTFS** 两种方式。弹框中选择挂载方式并填写参数，挂载命令随输入**实时生成**：

=== "NFS over RDMA"

    - 参数：**远端 IP**、**远端 fs 名**、**本地挂载目录**（默认 `/mnt/<fs 名>`，不存在时自动创建）；
    - 命令模板：`mount -t nfs -o vers=3,nolock,proto=rdma,nconnect=8 <IP>:/<fs> <本地目录>`

=== "DTFS"

    - 参数：**远端 CID**、**远端 fs 名**、**本地挂载目录**（默认 `/mnt/<fs 名>`，不存在时自动创建）；
    - 命令模板：`mount -t dtfs -o rw,eventual,cid=<CID>,dn=dns_local /<fs> <本地目录>`

点 **「💾 挂载」** 后工具通过 SSH 依次执行：`mkdir -p` 创建本地目录 → 执行挂载命令 → `mountpoint -q` 校验挂载点；成功后自动把本地目录**加入上方映射表**（类型可选 KV Cache / 模型 / 自定义，默认 KV Cache 并映射到容器内相同路径）。

### 3. 容器配置与生成 / 执行命令

容器参数（容器名 / 共享内存 / 网络）与 docker run 命令集中在同一模块配置：

| 参数 | 默认 | 说明 |
|---|---|---|
| 容器名 | `ucm-vllm` | — |
| shm-size | `512g` | UCM KV-cache 页缓存要求 ≥512 GiB |
| 网络 | `host` | Ascend 容器使用；NVIDIA 容器固定用 `-p 8000:8000` 端口映射 |

点「🔁 生成/刷新命令」自动生成 docker run。对 Ascend 服务器，工具会先探测宿主机实际存在的设备节点与驱动/工具/配置挂载，只生成存在的 `--device` 与 `-v` 参数：

- 兼容 `Ascend950PR` / A5：没有 `/dev/devmm_svm` 时不会生成该参数；
- 探测到 `/dev/ummu` 或 `/dev/uburma` 时自动添加 `--privileged=true`；
- 自动包含实际存在的 `/usr/local/Ascend/driver`、`/etc/ascend_install.info`、`/etc/hccl_rootinfo.json`、`/etc/hixlep`、`/usr/local/dcmi`、`/usr/local/sbin`、`/usr/local/bin/npu-smi`、`/usr/bin/urma_admin`（950 机型，只读）、`/root/.cache` 等挂载；
- **不挂载 `/usr/lib64`**——宿主机动态库覆盖容器内库目录会导致容器启动即退出。

=== "NPU（vllm-ascend）"

    ```bash
    docker run -itd \
        --name ucm-vllm \
        --entrypoint /bin/bash \
        --net=host --shm-size 512g \
        --device /dev/davinci0 ... --device /dev/davinci7 \
        --device /dev/davinci_manager --device /dev/hisi_hdc \
        --device /dev/ummu --device /dev/uburma \
        --privileged=true \
        -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
        -v /etc/ascend_install.info:/etc/ascend_install.info \
        -v /etc/hccl_rootinfo.json:/etc/hccl_rootinfo.json \
        -v /etc/hixlep:/etc/hixlep \
        -v /usr/local/dcmi:/usr/local/dcmi \
        -v /usr/local/sbin:/usr/local/sbin \
        -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
        -v /root/.cache:/root/.cache \
        -v /mnt/kvcache:/mnt/kvcache \
        -v /data/models/Qwen3-32B:/models:ro \
        vllm-ascend-ucm:v0.26.0rc1
    ```

=== "GPU（vllm）"

    ```bash
    docker run -itd \
        --name ucm-vllm \
        --entrypoint /bin/bash \
        --runtime nvidia --gpus all \
        -p 8000:8000 --ipc=host --shm-size 512g \
        -v /mnt/kvcache:/mnt/kvcache \
        -v /data/models:/models:ro \
        vllm-openai-ucm:latest
    ```

命令**可编辑**，点「🚀 创建容器」按编辑框内容执行；执行前会校验命令仍包含 `--entrypoint /bin/bash` 且未在镜像后追加 `bash`。完成后可点「✅ 检查容器内 UCM」确认。

## 4. UCM 文件配置（写入目标容器）

容器创建后，把 UCM 配置文件写入容器内（步骤5 拉起命令的 `UCM_CONFIG_FILE` 直接使用）：

- 编辑框**预填参考默认配置**（`UcmPipelineStore` / `Cache|Posix`），`storage_backends` 等参数按实际存储环境直接修改；
- **容器内路径**默认 `/workspace/ucm_config_example.yaml`，可自定义；
- 点 **「💾 写入容器」**：SFTP 上传宿主机临时文件 → 容器内 `mkdir -p` → `docker cp` 拷贝 → 读取文件开头校验 → 清理临时文件，全程任务面板可跟踪；
- 写入成功后路径自动记录，**步骤5 拉起命令中的 `UCM_CONFIG_FILE` 会同步使用该路径**（黄底高亮同步更新）；
- 详细参数说明见页面配置提示中的 [UCM 参数配置说明（华为官方文档）](https://support.huawei.com/enterprise/zh/doc/EDOC1100582752/b4b5937b?idPath=7919749%7C251366268%7C250389224%7C263153904%7C268354339)。

!!! note "下一步"
    容器创建成功后，进入[步骤4 · UCM容器环境复检](step4.md)在容器内做部署前检查。
