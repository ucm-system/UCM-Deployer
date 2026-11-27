# 步骤5 · 推理服务部署

页面按操作顺序给出两个模块——**推理服务拉起命令模板与注意事项**、**连通性验证（命令生成 + 工具自动执行）**。

UCM 配置文件已在**步骤3「UCM 文件配置」**写入容器（见该页说明）；本页拉起命令中的 `UCM_CONFIG_FILE` 路径与步骤3 写入路径**自动联动**（未写入时显示默认路径占位，需保持一致）。

## ① 推理服务拉起命令（在容器内执行）

注意事项：

1. 拉起服务前需先导出环境变量：`export ENABLE_UCM_PATCH=1`、`export VLLM_CPU_AFFINITY=1`；
2. 拉起命令后必须附加 `--kv-transfer-config`，通过 UCMConnector 把 UCM 接入 vLLM；
3. **PD 分离部署**时，`--kv-transfer-config` 只需在 **Prefill 实例**的拉起命令中添加，**Decode 实例无需添加**；单机 / 多机部署按原有方式（即下方模板）；
4. 其中 `UCM_CONFIG_FILE` 为 UCM 配置文件路径，指向步骤3「UCM 文件配置」写入的 yaml（写入成功后自动同步；详细参数说明见步骤3 页内链接）。

命令模板（模型路径 / 端口等按实际填写）：

```bash
export ENABLE_UCM_PATCH=1
export VLLM_CPU_AFFINITY=1

vllm serve /models/Qwen ... \
    --kv-transfer-config '{
        "kv_connector": "UCMConnector",
        "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {
            "UCM_CONFIG_FILE": "/workspace/ucm_config_example.yaml"
        }
    }'
```

## ② 验证连通性（命令生成 + 自动执行）

在输入框中填写服务**端口**（默认 `7800`）与**模型名**（如 `qwen3.8`，须与 `vllm serve` 的模型一致），下方验证命令**自动生成**。两种用法：

- **工具自动执行**：点 **「▶ 执行连通性测试」**，工具通过 SSH 在目标服务器上依次执行三条 curl，逐项判定 PASS/FAIL 并写入任务日志（失败时给出排查提示：服务就绪状态 / 端口一致性 / `--network host`）；
- **手工执行**：复制下方命令到宿主机另开的终端执行。

```bash
curl --fail http://127.0.0.1:7800/health
curl --fail http://127.0.0.1:7800/v1/models

curl --fail-with-body http://127.0.0.1:7800/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{
        "model": "qwen3.8",
        "messages": [
            {
                "role": "user",
                "content": "Explain the role of KV cache in large language model inference."
            }
        ],
        "max_completion_tokens": 512,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": false}
    }'
```

!!! tip "可复制 + 可拖高"
    三个模块的配置 / 命令展示框均可直接选中复制，右下角拖动手柄可调整高度，方便完整查看长命令。
