# 依赖、来源与发布边界

## 授权与范围

SPC 是独立实现的原创合成数据工程，包名 `spc_agent`，发行名 `spc-analysis-agent`，不 import 其他业务工程。发布校验设计参考原创 R3，但没有继承其领域、图依赖或模型验收结论。

本新工程**没有 LICENSE，也未作整体开源授权声明**。使用公开依赖、公开权重或可见源码不等于作者已授权再分发、商用或改许可。本文不是法律意见、完整 SBOM 或逐文件许可证审计；最终发行者仍须核对相应版本的 LICENSE、NOTICE、转换产物及数据权利。

## Python 与前端

- Python `>=3.12,<3.13`；构建后端 Hatchling `>=1.27`。入口为 `spc-analysis = spc_agent.cli:main`。
- 声明范围以 `pyproject.toml` 为准，锁定版本、公开下载地址、大小和哈希以 `uv.lock` 为准。当前解析记录为 31 个包，原环境安装记录为 30 个；这不是对新安装环境的计数承诺，平台条件和工程自身元数据会影响计数。
- lock registry 为公开阿里云 PyPI 镜像。哈希可约束字节一致性，但不证明供应链安全、许可证正确或源代码到二进制的可复现性。校验脚本不自动改源、不下载。
- 原生 HTML/JS/CSS 随应用 wheel 分发，必需资源为 `templates/index.html`、`static/app.js`、`static/style.css`。无外部 CDN、npm 构建、Java 或 JVM 依赖。
- **没有 LangGraph 是刻意设计**：SPC 统计与冻结基线确定性优先，受限调查由独立状态机执行。不为框架一致性强加图依赖。

主要直接依赖及开发工具的版本/许可边界如下；不是所有传递依赖的许可清单。

| 依赖 | 当前锁定版本 | 用途 | 上游许可边界 |
| --- | --- | --- | --- |
| FastAPI | 0.162.0 | HTTP API | MIT |
| Pydantic | 2.14.1 | 严格数据模型 | MIT |
| HTTPX | 0.28.1 | 本机 HTTP 协议与 TestClient | BSD-3-Clause |
| NumPy | 2.5.4 | 确定性统计与合成样本 | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` |
| Typer | 0.27.3 | CLI | MIT |
| Uvicorn | 0.54.0 | 本机 ASGI | BSD-3-Clause |
| pytest | 9.2.0 | 测试 | MIT |
| Ruff | 0.17.3 | 静态检查 | MIT |
| Hatchling | 声明 >=1.27；不冒充已锁定构建环境 | 构建后端 | MIT |

NumPy 的复合许可不可缩写成仅 BSD；其二进制依赖附带声明仍需独立保留。传递依赖以实际选定发行物为准，不把上表“其余 MIT”推广到整个依赖闭包。外部工具 uv 为 MIT 或 Apache-2.0 双许可；安装校验使用新 venv 自带的 pip，不将环境工具打入应用 wheel。

## 外部公开模型与引擎

只记录公开身份，不分发权重、引擎、本机动态库或路径配置。SPC 真实验收显式使用仓库外托管配置，由运行时核验 binary/libraries/generation 的实际文件身份；公开模型与引擎复用此前已核验的下列组合。仅调用 generation，不读取企业资料、不重新下载权重。SPC 自身的失败和成功记录见 [验收记录](validation.md)，不借用 R3 的调用结果。

| 组件 | 公开来源 | 固定身份 / 许可 |
| --- | --- | --- |
| llama.cpp | https://github.com/ggml-org/llama.cpp | `b6500`，提交 `a7a98e0fffed794396b3fbad4dcdbbc184963645`；MIT |
| Qwen3-4B-Instruct-2507 | https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507 | 原模型标示 Apache-2.0；目标量化为 Q4_K_M |
| Qwen GGUF 发行物 | https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF | revision `0b0406b39725d752255ffeb48f102c66f98e14aa`；转换/量化分发条款须另核对 |

固定文件 `Qwen3-4B-Instruct-2507-Q4_K_M.gguf`，大小 `2,497,281,120` 字节，公开参考 SHA-256：

```text
3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597
```

原记录使用公开 ModelScope resolve 镜像路径取得同一发行仓库/revision/file。下载者须独立确认镜像、上游模型卡及分发条款；校验脚本不自动下载。llama.cpp 二进制与库的哈希取决于具体本机构建，不能用源提交哈希代替二进制 SHA-256，也不能沿用另一机器的构建结论。SPC 只加载 generation，不依赖 BGE 嵌入或重排模型；兼容旧三角色配置的形状不等于使用其余模型。

## 发布与安装校验

所有命令在 SPC 项目根执行；以下文件名只示意当前版本的**已经由发布负责人冻结后构建**的产物，不是让校验脚本构建。

```bash
.venv/bin/python scripts/check_release.py
.venv/bin/python scripts/check_release.py \
  --archive dist/spc_analysis_agent-0.1.0-py3-none-any.whl \
  --archive dist/spc_analysis_agent-0.1.0.tar.gz
.venv/bin/python scripts/check_install.py \
  --wheel dist/spc_analysis_agent-0.1.0-py3-none-any.whl \
  --wheelhouse "$SPC_WHEELHOUSE"
```

`SPC_WHEELHOUSE` 由操作者显式指向源码目录外的、已审阅的平坦 wheel 目录；不能指向 runtime、模型或资料目录。必须预先具备适合当前 Python/平台的完整依赖闭包。不提供联网缺包补救、sdist 依赖构建、构建开关或上传功能。安装器离线解析该目录中的兼容 wheel，不声称自动按 `uv.lock` 锁定新环境；要复核原锁版本，发布者应只提供相应的经哈希核对发行物。

### 源码与归档边界

- 仅从精确 Git 根的 tracked + 非 ignored untracked 候选取得名单；不递归扫描父目录、虚拟环境或敏感 runtime。force-added ignored 文件直接拒绝且不读内容。
- 白名单为根 `.gitignore`、`README.md`、`pyproject.toml`、`uv.lock`，以及 SPC 的 `src`、测试、Markdown 文档和 Python/MJS 脚本的受限路径；校验脚本自身也在扫描范围。
- 路径禁止 traversal、控制字符、符号链接、硬链接和非普通文件；拒绝模型、二进制、凭据文件和未批准路径。最多 2000 个文件、单文件 2 MiB、累计 32 MiB；只读有限 UTF-8 文本。
- 内容规则检查私钥、常见 token、凭据 URL/query、长凭据字面量、个人目录路径、常见 CDN 和外部 Web 资源加载；处理有限次数的 URL 编码和 HTML 实体。仅输出规则名，不输出命中值。**无测试字面量例外**，拒绝测试应运行时构造输入。
- 这是 bounded heuristic，无法识别所有拆分/编码秘密、业务隐私或所有动态网络加载；需要人工审阅。不能将 PASS 当成完整秘密、法律或供应链审计。
- wheel 必须为纯 Python `py3-none-any`，发行身份/入口点正确，必需模块和三类资源齐全；RECORD 恰好覆盖 payload，并逐项验证 SHA-256/大小。包内文件须与候选源码字节完全一致。
- sdist 不解压；拒绝链接、特殊文件、重复/大小写碰撞成员和越界大小。仅移除生成的 `PKG-INFO` 后，文件集合与字节须完全等于源码候选，`PKG-INFO` 自身也扫描并核对发行身份。

### 安装证据与限制

安装校验先通过源/产物 gate，再复制已校验 wheel 到源码目录外的新临时目录，创建无 system-site-packages 的全新 venv，离线真正安装应用及 wheelhouse 依赖；执行 `pip check`。所有 venv/安装/probe 使用 `python -I`，环境不继承配置、token、代理或 PYTHONPATH。探针验证实际 distribution、安装文件哈希、所有指定模块、真实生成的 console script `--help`、fixture JSON、TestClient `/health`、首页与静态资源，并禁止模型会话、真实网络连接及子进程。

默认 fixture 对比 `baseline_dataset()`，可用 `--case mean_shift` 等当前 CLI 支持的 case；期望值来自安装包自身的 fixtures，绝不导入源树或旧包。临时产物退出后删除。

**source-free fresh venv 不等于 new OS/new host**，仍共享当前操作系统、Python 基础运行时和内核。本实现面向 POSIX/当前 Python 范围；不承诺 Windows 已通过。不隔离恶意宿主、不在不可信 wheel 上运行；Python audit hook 是误调用防线，不是恶意 Python 代码沙箱。

自动化 artifact 测试使用小型 ZIP/TAR 夹具，安装 subprocess 单元测试包含 mock；这些不替代真实安装。本工程随后已实际构建 wheel/sdist 并校验源码字节一致性，在源码外全新 venv 中离线安装成功：24 个依赖 wheel 逐一匹配 `uv.lock` SHA-256，`pip check`、真实入口点、模块、fixture、health 和页面资源探针通过。此安装探针没有真实模型推理，也不是新操作系统验收；真实 Qwen 的独立证据见 [验收记录](validation.md)。
