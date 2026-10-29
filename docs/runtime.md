# SPC 本地运行契约

## 确定性统计先行

统计结论由数值引擎、经显式人工批准的冻结基线和受校验测量决定；本地模型不能重写统计事实。提示词要求模型只提出待核验假设和人工审阅建议，但代码不对草稿做语义真实性证明：模型仍可能产生错误或过度确定的叙述，所以文本隔离在 `untrusted_model_draft`，不能提升为权威结论。工具边界不提供措施执行、SQL、Python、命令或工艺参数修改能力。

本工程无 LangGraph、Java、BGE 检索链要求。业务 fixture 完全原创合成；安装校验不读取生产资料、现有 runtime 配置或模型文件，也不作真实推理。公开模型身份和许可边界见 [依赖说明](dependencies.md)。

## 显式启动，不读取环境中的运行配置

当前 CLI 先显式初始化一个新数据目录，再对 `serve` 恰好选择一种 provider；不能同时选择或默认回退。

```bash
spc-analysis fixture
spc-analysis fixture --case mean_shift
spc-analysis init ./.spc-data

# 以下三种 serve 模式只能选一种；变量由操作者安全设置。
spc-analysis serve ./.spc-data --runtime-config "$SPC_RUNTIME_CONFIG"
spc-analysis serve ./.spc-data --model-url http://127.0.0.1:18080
spc-analysis serve ./.spc-data --test-mode
```

这里的 `SPC_RUNTIME_CONFIG` 仅为 shell 展开变量示意，**应用不从环境自动发现该配置**；必须显式传 `--runtime-config`，路径须绝对。真实配置位于仓库之外，不提交、不打包、不贴公开 issue，不在文档中复制个人路径。初始化会显示一次管理员 token，后续认证使用隐藏 prompt；不要把 token 放命令参数、日志、文档或测试字面量。

HTTP 监听默认 `127.0.0.1:8004`，CLI 只接受数字回环监听地址。`/health` 返回 provider 模式/指纹和 `inferenceChecked: false`；只证明应用可响应，不证明模型已加载或可推理。`--test-mode` 明确为 synthetic-test，不是 LLM，也不是故障回退。

## 外部已启动服务

`--model-url` 使用 `LocalProvider`。只接受数字回环 HTTP（IPv4 或 IPv6），拒绝主机名 `localhost`、远端地址、凭据、query、fragment、非规范路径；不信任环境代理、不跟随重定向、不自动重试，无云端或 synthetic 回退。

生成协议为 `POST /v1/chat/completions`，模型别名 `qwen3-4b-instruct-2507`，温度 0，严格 JSON schema，`max_tokens = 768`，`cache_prompt = true`。必须返回完整 stop 结果和有效 `usage.total_tokens`；截断、额外工具调用、未知 ID 或字段校验失败不能视为成功。

外部连接模式不拥有服务生命周期，不能证明该服务加载的模型字节身份，也不能保证其他调用者没有共享其缓存。仅有相同模型别名不等于哈希验证。需要受控进程生命周期时应显式使用 ManagedLocalProvider，而不是把外部服务当作已托管。

## ManagedLocalProvider 配置

配置由可信本地操作者显式提供，JSON 不超过 64,000 字节，严格字段和类型校验；不支持任意 shell、下载命令或递归配置。接受以下形状，不提供包含本机路径或伪哈希的可直接运行示例。

| 字段 | 约束 |
| --- | --- |
| `binary` | `{path, sha256}`；llama-server 可执行文件，64 位小写十六进制 SHA-256 |
| `libraries` | 1–30 个 `{path, sha256}`；与 binary 同目录 |
| `models` | 首选恰好 `generation`；也可兼容恰好 `generation`、`embedding`、`rerank` 的旧三角色形状 |
| model record | `{path, sha256, bytes}`；bytes 为真实正整数 |
| `lock_path` | 已有父目录下的本地锁文件路径 |
| `port` | 默认 18080，范围 1024–65535 |

所有配置内路径须绝对、彼此不同，无 traversal/控制字符。只读取并校验 binary、libraries、generation 模型；旧三角色配置中的 BGE 字段仅校验结构，**不打开、不哈希、不加载、不调用 embedding/rerank 文件**。配置构造本身不启动模型；首次决策才惰性启动。

公开目标组合为 Qwen3-4B-Instruct-2507 GGUF Q4_K_M 与 llama.cpp `b6500` CPU。固定模型 revision/大小/hash 见依赖说明。运行配置中的哈希验证的是操作者选择的文件一致性，不自动证明该选择符合公开目标、许可证合法或模型行为正确。

### 文件身份和进程边界

- 首次使用及文件身份变化后，校验 generation 大小及 SHA-256、binary 与动态库 SHA-256；校验普通文件和路径链无符号链接。身份缓存绑定设备/inode/大小/修改时间等，不跳过变化后的完整性核对。
- 显式 CPU 参数包括 `--n-gpu-layers 0 --device none --no-op-offload --flash-attn off`。不是 GPU 失败后的静默回退。
- 上下文 8192，并行度 1，CPU 线程 4，seed 42；只绑定 `127.0.0.1`，关闭引擎自带 Web UI、slot 页面及日志。
- 参数数组直接交给 Popen，不经 shell；使用临时 HOME/TMPDIR 与最小环境，不继承 token/代理。线程租约、单调用锁及 `fcntl` OS 文件锁限制重叠进程；占用端口被拒绝，不将未知本机服务冒充目标引擎。
- `session()` 只包围一次调查 `run` 的调用范围：一次 run 首次决策惰性加载，后续决策可重用该进程与 prompt/context cache；不是跨人工暂停、跨持久化会话或跨 run 的常驻 daemon。退出 run、失败或取消后执行进程清理，独立 decision 自动建立自己的短作用域。
- startup 等待名义上限 90 秒，生成传输超时 180 秒，响应还有字节/阶段性 deadline 检查；均不应误写成整个运行严格实时上限。清理先 terminate/wait，必要时 kill/wait；无法确认回收时该 provider 实例锁定为 cleanup-unknown，拒绝新推理。普通调查 API 对账不会解除这个进程安全锁；操作者必须先独立核实并清理对应进程，再显式重启服务建立新实例。
- ManagedLocalProvider 使用 POSIX `fcntl`，尚无 Windows 兼容性承诺。

## 工具、计费与 CPU 预算

模型只可从当前调用者发放的菜单选一步：`get_process_context`、`get_measurement_context`、`get_action_history` 或 `submit_investigation`。三个 read tools 参数必须为空、各只允许一次；提交由后端根据当前证据覆盖打开，引用只能使用已发放 ID。数据/证据视为不可信内容，不转成命令。提交仍是未审阅草案，业务侧独立核验工具资格、引用、时效和权限。

`MAX_TOKENS = 768` 是单次生成输出上限，**不是总 token 预算或总计费量**。每次使用响应的 `usage.total_tokens` 全额记账（包含输入和输出）；prompt cache 命中不意味着可自行扣除输入 tokens。先读取 usage，再校验响应/动作，因此无效草案已知消耗也记录。仅能确认未发送时记 0；已跨传输边界但无法确定结果/usage 时明确 unknown，不虚构为零，不自动重试。

调查的 `MAX_SECONDS = 300` 是 CPU 场景中的**协作式业务时间预算**：步骤/回包后检查累计耗时，超预算拒绝继续提交，不是 300 秒到点对正在执行的 HTTP 请求或进程硬 kill 的独立 watchdog。启动、阻塞传输和清理可能使墙钟耗时超过 300 秒；不要把该常数当作 SLA 或硬实时截止。进程清理的 kill 升级仅用于退出回收，不等于业务预算的硬终止。

## 校验事实与未覆盖范围

`check_release.py` 只扫描公开候选与显式归档，`check_install.py` 只验证已审阅 wheel 在源码之外新 venv 中的实际安装、入口点、合成 fixture、模块和本地 TestClient 页面/静态资源。安装探针不会实例化 ManagedLocalProvider，不读取 runtime 配置，不启动模型；health 成功不等于推理成功。

校验仅接受明确已审阅的自有 wheel 与本地依赖 wheelhouse，不提供构建、上传、自动联网补依赖或 Java 检查。源码级测试通过以及 mock 安装流程通过，都不能替代代码冻结后执行真实发行包构建和完整新 venv 安装。source-free fresh venv 仍共享当前宿主 OS，不得报告为新操作系统验收。

SPC 已在当前 macOS/Apple M1 的仓库外托管 CPU 运行时完成真实 Qwen 验收：一次均值偏移风险调查实际 3 次模型调用，随后独立人工合成措施闭环关单。首轮失败与第二轮通过的记录见 [验收记录](validation.md)。没有使用 BGE、云服务或企业资料；不外推 GPU、跨平台、现场统计效果或生产可靠性。
