# 平台兼容性目标与验收

以下是版本下限与待验证矩阵，不是“所有列出的系统均已通过”的声明。后续系统版本不能仅凭版本号自动获得应用兼容性结论。

## 目标与执行入口

|目标|架构边界|测试入口|证据边界|
|---|---|---|---|
|Windows 11 及以上|x64|本机测试；`platform-targets.yml` 的 `windows11`|必须是 workstation；Windows Server runner 不替代 Windows 11|
|macOS 26 及以上|arm64、x64|`test.yml` 的 `macos-26`、`macos-26-intel`|配置了 native/fallback × CPython 3.10/3.13，需实际 CI 回执|
|Ubuntu 26.04 LTS 及以上|x64|hosted `ubuntu-26.04`；手动 `ubuntu26`|必须另核验实际内核不低于 6.18.56|
|Debian 13 及以上|x64|手动 `debian13`|必须另核验实际内核不低于 6.18.56|
|Fedora 44 及以上|x64|手动 `fedora44`|同上；不以 `ID_LIKE` 冒充指定发行版|
|Fedora 45 及以上|x64|手动 `fedora45`|独立版本门，Fedora 44 不能满足此 lane|

发行版名称、内核下限及发布状态是不同事实。目标版本未取得可运行镜像或可信机器时保持“待验证”，不编造发布状态或运行结果。Linux arm64、32 位、PyPy、free-threaded Python、未来 Python 版本不在本次矩阵中。

`windows-2025` 仅保留为 Windows Server 回归 lane。GitHub 官方 runner 标签见 [runner-images](https://github.com/actions/runner-images#available-images)；标签不能替代回执中的实际 OS、架构与内核。

## 版本探针

```bash
python scripts/platform_check.py --output /path/to/new-evidence/platform.json
python scripts/platform_check.py --require-target --expected-system Linux --expected-distro debian --minimum-distro-version 13 --output /path/to/new-evidence/platform.json
```

探针仅用标准库，不加载 `.env`、业务配置、数据库或模型。不符合目标、版本未知或工作站类型未知时严格模式失败；读取失败原样报错，并先删除旧输出，防止旧成功回执冒充本轮结果。WSL/容器可作补充回归，但不能获得目标主机资格；容器内核属于宿主。缺少容器标记也不是独立物理隔离证明。

版本检查不参与日常启动阻断，不把低于目标版本但原本可运行的环境改为强制退出。

## 原生扩展与隔离测试

安装器的不可变版本目录、活动指针、显式版本选择、失败保护保持不变。Windows 使用 `vector_lake_core.pyd`；macOS/Linux 使用 `vector_lake_core.so`。Unix 构建复用 `ci_core.py` 的 Maturin wheel 路径，以保留正确的平台链接参数；只在校验构建回执中的单一 `.so` 路径与 SHA 后写本地构建产物，再由原有安装／新进程探针激活。

构建工具先在测试虚拟环境安装，不修改共享 Python：

```bash
python -m pip install --index-url https://pypi.org/simple --only-binary=:all: --no-deps --require-hashes -r scripts/ci-build-requirements.txt
python scripts/ci_core.py build --output /path/to/unused-native-build --timeout 900
python scripts/ci_core.py test --backend native --native-dir /path/to/unused-native-build --receipt /path/to/new-evidence/native.json -- -q
python scripts/ci_core.py test --backend fallback --receipt /path/to/new-evidence/fallback.json -- -q
```

上述测试门强制绑定本 checkout 的 Rust 输入、实际原生文件路径／哈希与开始／结束后端，不接受碰巧装在主机上的旧扩展。新增真实安装测试仅在 explicit native lane 中复制该文件到临时 site-packages，检查 fresh-process 激活及缺失版本失败保护；不改真实 site-packages。

测试通过仍不等于真实模型可用、完整生产摄取成功、服务托管验收或 SLA。需要时另在 synthetic MEMORY 上运行 `scripts/business_acceptance.py`，不能指向正式 MEMORY。不得因本次平台测试重启现有 watchdog/runner、执行数据库迁移、清队列或修改生产配置。

## 专用目标机器与安全边界

`platform-targets.yml` 只允许 default branch 上人工 `workflow_dispatch`，不由 push、PR 或 fork PR 自动调用 self-hosted。当前修改没有注册 runner、创建虚拟机、调整内核、推送代码或远端执行测试。

启用前由所有者完成：

1. 提供专用、可销毁、无业务数据／私人凭据的目标 VM，隔离网络与宿主资源；禁止绑定当前服务主机或含私人凭据的普通工作站。venv 与 runner 标签不是安全沙箱。
2. 只为该可信仓库授权，配置 runner group／仓库权限；启用人工分发前核对 default branch 的审核／保护策略。工作流自身不能替管理员建立这些物理与账户边界。
3. 安装 Git/Bash、指定 CPython 3.13、兼容 Rust/Cargo/MSVC 或 Unix 编译工具链；Linux VM 提供满足内核下限的实际内核。不授权自动升级宿主内核。
4. 按工作流固定标签注册 `vector-lake-ephemeral` 及对应 `vector-lake-windows11`、`vector-lake-ubuntu26`、`vector-lake-debian13`、`vector-lake-fedora44`、`vector-lake-fedora45`。不要因队列等待而把凭据主机补成同名标签。

手动矩阵串行执行；每个 lane 使用带 run/attempt/target/backend 的独立虚拟环境及构建目录，不向共享 Python 装依赖。测试结束由宿主销毁流程清理 VM。平台／后端 JSON 同时写入 job 临时目录并输出到 CI 日志，日志保留期由仓库策略控制；不新增上传 action，不发布编译 wheel、源材料、数据库或凭据。新增 macOS build/test wheel SHA 来自对应版本的官方 PyPI JSON；没有升级这些包的版本。哈希不等于发布者签名、无漏洞证明或法律审批；macOS 的实际安装依赖树和运行结果仍待目标平台验收。

## 服务托管不等同于代码可运行

Windows 的 Task Scheduler / Job Object 脚本保持 Windows 专属。macOS/Linux 的应用入口与 POSIX 进程控制需要在目标主机测试；systemd/launchd 的注册、开机／注销恢复、休眠与断电恢复必须另验，不能把 Windows 计划任务结果外推。此处不安装服务，也不预先声称这些能力已通过。
