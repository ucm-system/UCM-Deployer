# 步骤5 · 服务部署

**纯操作指引页**：页面按操作顺序给出三个模块——UCM 配置文件参考配置、推理服务拉起命令模板与注意事项、连通性验证命令。所有内容可直接复制，在容器内手动完成后按模块3 验证服务是否就绪。

## ① UCM 配置文件

页面给出 UCM 配置文件参考默认配置（`UcmPipelineStore` 连接器，`Cache|Posix` 存储管线），`storage_backends`、`posix_capacity_gb`、`posix_io_engine`、`posix_data_trans_concurrency` 等参数请**按实际存储环境修改**：

```yaml
ucm_connectors:
  - ucm_connector_name: "UcmPipelineStore"
    ucm_connector_config:
      store_pipeline: "Cache|Posix"
      storage_backends: "/mnt/test"
      posix_capacity_gb: 10240
      posix_io_engine: "psync"
      posix_data_trans_concurrency: 128
```

- 将配置保存为 yaml 文件（如 `/workspace/ucm_config_example.yaml`，路径可自定义）；
- **路径需与拉起命令中 `UCM_CONFIG_FILE` 保持一致**；
- 详细参数说明见页面配置框下方的 [UCM 参数配置说明（华为官方文档）](https://support.huawei.com/enterprise/zh/doc/EDOC1100582752/b4b5937b?idPath=7919749%7C251366268%7C250389224%7C263153904%7C268354339)（链接可点击跳转）。

## ② 推理服务拉起命令（在容器内执行）

注意事项：

1. 拉起服务前需先导出环境变量：`export ENABLE_UCM_PATCH=1`、`export VLLM_CPU_AFFINITY=1`；
2. 拉起命令后必须附加 `--kv-transfer-config`，通过 UCMConnector 把 UCM 接入 vLLM；
3. 其中 `UCM_CONFIG_FILE` 为 UCM 配置文件路径，指向①中保存的 yaml（可自定义修改，两处路径必须一致）。

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

## ③ 验证连通性（服务就绪后，在宿主机另开终端）

端口 `7800` 与模型名 `qwen3.8` 按实际替换：

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
