# Rust 核心：预编译与免编译安装

普通部署不需要 Rust、Cargo 或 maturin。构建机编译一次，用户在**新建的项目虚拟环境**中安装匹配的预编译 wheel；缺包、平台不兼容或校验失败就退出，不临时下载编译器或回退源码编译。

## 当前交付边界

- Windows x64 0.2.5 包已由已有、输入哈希匹配的构建导出，并在没有 Cargo/rustc/maturin 的新环境中实测安装。
- `.github/workflows/native-wheels.yml` 配置 Windows x64、macOS arm64/x64、Linux x64 自动构建和隔离安装验证。远程工作流尚未执行，不能视为这些平台已通过。
- **尚无公开 Release/PyPI 下载渠道。CI 当前只生成任务临时目录中的包，不上传或发布，任务结束后不能下载它。** Windows 本地包可以由获授权的所有者供可信本地测试使用；其他平台需先执行实际构建。
- 尝试引入的上传 action 在锁定依赖的公开漏洞查询中有版本匹配，尚未核清可达性，因此没有加入最终工作流。这不是已证实利用，也不是对所有上传方式的否定。分发渠道应另行审查接入。
- 包中的 SHA 和源输入绑定用于一致性检查，不是发布者签名、可复现构建证明或第三方许可批准。当前包仅用于可信私有/本地测试，不是批准对外再分发的发行包。

## 部署者：只安装，不编译

先从可信所有者取得与当前 checkout 的 Rust 输入和操作系统/架构匹配的目录，内含一个 `.whl` 和 `native-bundle.json`。不要安装未知来源的二进制代码；从同一不可信渠道得到的哈希无法证明身份。

Windows，在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe scripts/native_bundle.py install --bundle C:/path/to/native-bundle --receipt C:/path/to/install-receipt.json
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
```

macOS / Linux 的相应入口（须先取得真实目标构建的包，不能使用 Windows wheel）：

```bash
python3 -m venv .venv
.venv/bin/python scripts/native_bundle.py install --bundle /path/to/native-bundle --receipt /path/to/install-receipt.json
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
```

安装入口仅安装原生核心，不安装项目其他依赖、不配置模型、不初始化数据库、不切换版本指针、不启动或重启服务。后续服务必须显式选择新解释器并单独验收。

安装过程验证 wheel/原生文件 SHA、源输入、版本和新进程加载路径，并做合成 Unicode 调用；使用离线、仅二进制、哈希强制的 pip 安装。pip 配置和索引/依赖解析不能把该步骤改成源码安装。Python 至少 3.10；源输入检查读取仓库文件，不运行 Rust 工具链。

已安装核心的环境会被拒绝，避免覆盖正在加载的 DLL。升级也使用**新的 venv**；不要先删旧环境或恢复数据库。失败可能留下未通过探针的新环境，不能据此启用服务；查明失败后重新创建隔离环境。成功回执不能替代业务、平台、权限、恢复或 SLA 验收。

## 构建者 / CI：一次构建，导出包

只在开发/CI 构建机安装 Rust 和既有哈希锁定的 maturin：

```bash
python scripts/ci_core.py build --output /new/path/native-build --timeout 900
python scripts/native_bundle.py bundle --native-dir /new/path/native-build --output /new/path/native-bundle
```

构建器沿用现有 `--release --locked` 流程及源输入/产物回执；导出器验证构建回执、wheel 与原生载荷一致，拒绝覆盖已有输出目录。现有不可变版本规则不变，不覆盖旧包或已加载库，也不重新标记不同版本的字节。

自动工作流在相关 push / pull_request 或手动 dispatch 时编译，并在全新 venv、无 Rust 工具可执行路径及无 maturin 的条件下验证用户安装。当前没有新增上传 action 或发布凭证；配置与本地 YAML/脚本测试不是 GitHub 远程执行证据。

Windows Server 构建机不替代 Windows 11 运行验收。Linux 使用较旧 Ubuntu 构建用户空间降低 glibc 下限，由 maturin 决定实际 wheel 标签；不能仅凭 `linux` 标签宣称 manylinux、跨发行版或目标内核兼容。macOS 分架构构建，不声称 universal2。目标机器要求和剩余验收见 [PLATFORM_SUPPORT.md](PLATFORM_SUPPORT.md)。

公开分发前另需：可信发布渠道与身份、完整许可/NOTICE 材料审查、真实目标平台执行和发布授权。不要把现有本地可信数据使用授权扩大为上述批准。
