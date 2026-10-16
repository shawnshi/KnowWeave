# 开发、测试与 CI

所有命令和源码路径均相对于仓库根目录；先使用 README 核对过的项目虚拟环境。本文是参考，不是可整段执行的安装脚本。

[返回项目首页](../README.md)

## 开发环境

应用运行依赖与测试依赖分开；本地固定开发清单按已验证的 Python 3.13 环境使用，运行时支持 ≥3.10 不代表每个固定测试依赖都支持所有版本。其他解释器按实际 CI 清单/工作流创建独立环境。先在开发 venv 安装二进制依赖，不修改部署环境或全局 Python：

```powershell
python -m pip install --only-binary=:all: -r requirements.txt
python -m pip install --only-binary=:all: -r requirements-test.txt
```

`requirements-test.txt` 固定本地开发测试依赖；CI 的专用哈希清单独立维护，不表示 NumPy 是应用运行时必需依赖。native 构建工具只安装在构建环境，普通部署只安装可信预编译包。

平台矩阵以 `.github/workflows/test.yml` 为准；手动目标主机矩阵、原生 wheel 构建各自有独立工作流。不要从配置、标签或版本门推导目标主机已通过。

## CI Backend & Platform Gates

`.github/workflows/test.yml` 配置 Windows Server 2025／Ubuntu 26.04／macOS 26 arm64／macOS 26 x64 × `native`／`fallback` × Python 3.10／3.13 共 16 个组合，最多并发两个、每 job 25 分钟；仅授予 contents read，不保存 checkout 凭证。配置存在不等于具体发布提交的远端 16 个组合已通过。

* 两后端的既有 metadata／two-stage-index 测试需要 NumPy；`scripts/ci-test-requirements.txt` 固定 2.2.6（Python ≥ 3.10）、相应平台／解释器的 wheel 哈希，CI 仅安装官方二进制且不解析额外依赖。该包不加入应用运行时清单，不以本机偶然已安装来替代测试依赖声明。
* `native`：CI 专用清单固定构建工具 maturin 1.15.0 与适配 CI 平台/架构的官方 PyPI wheel SHA-256；Python < 3.11 的必需依赖 tomli 2.3.0 单列 marker 与官方 pure-Python wheel 哈希，不依赖偶然传递安装。不增加应用运行时依赖，不升级宿主安装。`scripts/ci_core.py build` 使用 release／locked、全新任务目录构建并提取 wheel，记录构建前后 crate 输入与 DLL／SO 哈希，不 pip install 项目原生模块。测试开始／结束均核验当前源码、任务内二进制、实际加载路径及 `indexer.HAVE_CORE`；缺失、过期、哈希错误或误用宿主模块均失败，不静默回退；任何后端校验前先使旧 probe 失效，失败不能遗留上次的成功回执。当前 checkout 的 graph／prepared PPR ABI 必须具备，不能把缺失 ABI 的 native-only 测试静默 skip。
* `fallback`：测试进程在导入应用前阻止 `vector_lake_core` 导入，开始／结束确认 `HAVE_CORE=False` 且没有加载 core；这不表示 sqlite-vec／Tantivy／rjieba 等其他原生依赖也被禁用。两条路径均禁用自动 pytest 插件和字节码缓存（CLI 也显式关闭当前解释器写入），并使用隔离 bootstrap MEMORY、清除 provider key 与 DB override。fallback 明确跳过只测试原生 API 的模块，单列 skip／collection 差异，不把减少的用例数称为相同全量覆盖。
* 可移植小图测试覆盖实际后端的直接／双向链接、来源／邻居证据、阈值与 NaN、零贡献桶。64 节点密集图把 `ci_core_*` 剪枝观测写入 probe JSON 与 stdout，并在两后端强制每节点最多 15 条 incident 边。core 0.2.2 修复旧版分离 source／target 计数和同权不稳定次序；Python 原生调用不再硬编码 50。新增 raw API 的 cap 0／1／2／5／15／64 与反向输入验证。同权小图边集合一致不是所有评分／证据归一化的 parity 证明；本次不改评分、舍入或证据资格规则。
* 本地可用 `python scripts/ci_core.py build --output <全新任务目录> --offline --timeout 150`，再用 `python scripts/ci_core.py test --backend native --native-dir <构建目录> --receipt <任务内probe.json> -- -q` 或 `--backend fallback` 核验。online 构建只面向已授权的隔离 CI runner；本地验证不触发 hosted workflow、不安装到主环境、不读生产库。


## Validation

README 的命令示例、MCP 工具表、依赖说明及配置项可通过定向测试核对；测试使用临时 MEMORY 根，不读写本机知识库：

```powershell
$env:PYTHONUTF8='1'; python -m pytest -p no:cacheprovider -q tests/test_command_surface.py tests/test_registries.py tests/test_dependency_manifest.py tests/test_portability.py tests/test_mcp_surface_improvements.py tests/test_runner_supervision.py tests/test_ingest_backends.py tests/test_ingest_model_seam.py
```

完整测试与实例运行检查（`doctor`、`search`、`debt` 面向当前配置的知识库，不是隔离测试）：

```powershell
$env:PYTHONUTF8='1'; python -m pytest -p no:cacheprovider -q      # pytest.ini 仅发现 tests/；CI 核心后端使用独立固定清单
$env:PYTHONUTF8='1'; python -m compileall -q vector_lake tests
$env:PYTHONUTF8='1'; python cli.py doctor
$env:PYTHONUTF8='1'; python cli.py search "<keyword>" --domain General --mode memory --top_k 3
$env:PYTHONUTF8='1'; python cli.py debt --top 1
```

检索示例中的 `General` 是实际类别，不代表全域；执行前须按任务授权选择明确域，不能省略 `--domain`。测试数量、语料规模和运行时健康度会随实例变化，应执行当前验证命令。历史变更和评测记录见 CHANGELOG 与 Git 历史，不作为当前健康度或质量保证。


[原生 wheel 构建与分发边界](NATIVE_WHEELS.md)；[目标平台与执行证据边界](PLATFORM_SUPPORT.md)。
