# macOS 离线候选交付

这是可检验的 Intel 与 Apple Silicon `.app` 离线候选；本轮在 Windows 上制作并检查二进制格式、依赖闭包、哈希、ZIP 和 Unix 权限。尚无真实 Mac 的 Finder、Python 首启、PG、Safari、签名或公证通过记录，不能作为已完成的 Mac 端到端验收。

## 使用条件与入口

候选包含 PostgreSQL 18.6 服务、initdb、pg_ctl、pg_dump、pg_restore、psql、pg_trgm 和动态库；不需要自行安装 PostgreSQL、Homebrew、Docker、Node、编译器或执行 pip。首次启动会在本机私有目录使用随包 wheels 自动创建 venv。用户只需要一次性安装 python.org 的标准 64 位 CPython 3.14.8 或更高的 3.14 补丁版本；free-threaded、debug 和 3.15 等其他次版本不适用。

Intel 选择 `macos-x86_64-cp314.zip`，Apple Silicon 选择 `macos-arm64-cp314.zip`。解压整个目录后打开 `综合资料库.app`；打开 `关闭综合资料库.app` 可以受控关闭该应用的后台服务。两个 `.app` 保持在同一目录。启动器使用系统 shell 调用本机 Python，并明确用本机 Safari 打开回环地址；关闭 Safari 页面不自动停止后台应用。

PG 核心 Mach-O 的最低系统标记为 macOS 13.0，APSW wheels 分别标记 Intel 10.15+ 和 arm64 11+。本候选的静态最低交集是 macOS 13；实际支持下限仍待各架构原生验收。Apple Silicon 的 Python 应以原生 arm64 运行，与该包的 wheel 架构一致。

当前包未由本发布方签名、公证或 staple，Gatekeeper 分发体验尚未通过。本候选不会自动修改系统安全设置或移除 quarantine。正式顺畅分发需要发布方在自己的 Mac 发布环境完成签名、公证和断网首次启动验收。

## 程序、数据与缓存

`.app/Contents/Resources/Program` 是程序目录，启动器不在其中初始化资料或 PGDATA，也不携带跨机器 venv。

| 内容 | macOS 默认目录 |
|---|---|
| 持久资料库、原件和 PGDATA | `~/Library/Application Support/ReferenceLibrary/Library` |
| PGDATA | 上述目录中的 `catalog/pg18-main` |
| 机器配置 | `~/Library/Application Support/ReferenceLibrary/Runtime/<LibraryUUID>/machine-selected.json` |
| 私有运行状态、venv 和运行日志 | `~/Library/Application Support/ReferenceLibrary/Runtime/<LibraryUUID>` |
| 可重建 SQLite 缓存 | `~/Library/Caches/ReferenceLibrary/<LibraryUUID>` |
| Finder 入口错误日志 | `~/Library/Application Support/ReferenceLibrary/LauncherLogs/finder-launcher.log` |

原件和 PostgreSQL 是持久内容，不能按运行状态或缓存清理。程序、资料、私有运行状态与缓存路径必须互不重叠。已有目录的归属和数据库身份不一致时启动器拒绝覆盖。应用实例锁同时绑定持久资料库与运行状态；只检查、启动或关闭自己的 PG。Mac 进程核验使用 libproc 获取真实执行路径及开始时间，避免只凭 PID 存在判定归属。凭据与机器绝对路径只写本机私有目录。

## 实际依赖与来源

| 组件 | 此包的实际版本或实现 |
|---|---|
| 主机 Python | CPython 3.14.8+，标准 cp314 |
| PostgreSQL 服务与工具、libpq | 18.6，EDB `18.6-5-osx-binaries.zip` |
| Psycopg | 3.3.6，pure Python fallback，绑定本应用 libpq 18.6 |
| Psycopg Pool | 3.3.3 |
| APSW / SQLite | 3.54.0.0 / 3.54.0，官方 cp314 各架构原生 wheel |
| Python-Markdown | 3.11.1 |
| typing_extensions | 4.16.0 |
| tzdata | 2026.5 |
| EDB 内含 OpenSSL | 3.5.9：当前 3.5 LTS 维护版；整体最新稳定版为 4.0.3 |
| EDB 内含 ICU | 77.1，未替换为其他主版 |

因此直接应用组件采用指定的最新稳定版，但**不宣称整个 EDB 包的所有传递依赖都是整体最新主版**。动态库主版不能单独替换后假定 ABI 相容。

Psycopg 官方说明 pure Python 比 C/binary 版本慢。本候选保留客户端 libpq 18.6 的准确绑定及无需编译的离线首启能力，属于功能候选；不能据此宣布大规模性能通过。官方 `psycopg-binary` 带自己的客户端库，本候选没有静默用其中的较旧 libpq 替换 18.6。正式性能候选需要在真实 Mac 上生成 psycopg-c 3.3.6 与此 libpq 的匹配 wheel、核验动态依赖并比较实际负载。

EDB 原始 ZIP 为 448,233,137 字节，SHA-256：

`a4147b2b9476a9d64f0b3ecc0112ca5ed3dc8eb217331e5b519fbdc608f7d30b`

官方 getfile 地址真实重定向到上述 ZIP。选定 PG payload 的动态库别名已经是完整的 regular Mach-O 文件，没有把 symlink 文本误当作 dylib。181 个选定动态程序/库均含 x86_64 与 arm64 两个 slice。PG 必需的非系统动态依赖均能在随包 lib 找到；Apple 的 `/usr/lib` 与 `/System/Library` 是系统依赖，需要本机验证。

移除了未使用的 pgAdmin、StackBuilder 应用及依赖额外 EDB Language Pack 的 PL/Python、PL/Perl、PL/Tcl 和相关转换扩展。应用只用原生 SQL、plpgsql 和 pg_trgm。完整官方源 ZIP留在构建下载目录；所有命名的 license/licence/copying/copyright 文件和上游第三方许可汇编保留在 runtime 中。`runtime/postgresql/macos-universal2/manifest.json` 记录每个交付文件的原成员、字节数、SHA-256、Unix 模式、CPU slice 和动态依赖；`wheels/macos-*-cp314/manifest.json` 记录 PyPI 来源、原生 tag 与固定版本；`release-manifest.json` 标记原生运行和签名未完成。

## 可复现构建与原生门槛

开发构建使用 `packaging/macos/build_app.py --arch both --output <全新绝对输出目录>`。它保存具体 `.app` 文件树，ZIP 中显式写入 Unix 0755 可执行程序和目录、原厂文件模式及 0644 普通文件；完成后核验 ZIP CRC、每个 payload 的 SHA-256 和模式。程序变化后应生成新的快照，不能把早期候选当作最终应用代码。

在两个真实 Mac 上，使用已安装的本机 Python 执行 `packaging/macos/native_smoke.py --output <本机私有证据.json>`。此脚本创建临时合成集群，验证版本、原生 import/libpq/SQLite 及 standalone PG/pg_trgm，完全不接触默认资料库；该检查仍不能代替 Finder 与 Safari。

开发者的 `packaging/macos/build_native_driver.py --output <全新绝对输出目录>` 使用 `build-support` 中固定哈希的 psycopg-c 3.3.6 源码和离线构建工具创建独立开发 venv，以随包 pg_config、头文件和库编译当前 Mac 架构的 cp314 C wheel，执行 `PSYCOPG_IMPL=c`、`pq.version()==180006` 导入验证并保存 wheel 哈希和日志。Mac 主机必须已有 Xcode Command Line Tools；这是开发者构建前提，不是用户运行要求。此环境缺失，所以本轮没有 C wheel 成功产物，也没有冒充跨编译完成。脚本不自动将 wheel 提升为正式包。

离线开发工具固定为 pip 26.2.1、wheel 0.48.0、packaging 26.3，以及上游 psycopg-c 3.3.6 的 `pyproject.toml` 明确要求的 `setuptools==83.0.0`。setuptools 整体最新为 84.0.0；此处采用上游明确兼容约束，不把构建工具 pin 偷换为日常应用组件版本，也不擅自修改上游源码。

原生 C wheel 构建通过后，可以用 `compare_driver_performance.py --python <开发 venv/bin/python3> --output <私有结果.json>` 在同一 Mac 上比较 Python / C / C / Python 四轮。该脚本仅创建临时合成集群和临时表，验证两种实现都绑定 libpq 18.6，记录每轮 100 次查询的 median、p95 和原始样本，再受控关闭。它是驱动层微比较，不能替代应用导入、检索、阅读器和大规模端到端测试；本轮没有原生比较结果。

`sign_and_notarize.sh` 仅用于发布方自己的真实 Mac、既有本地 Developer ID Application 身份及 notarytool keychain profile。它逐个签署 Mach-O，再签应用、verify、submit、staple、validate 并执行 Gatekeeper assess。当前没有这些执行证据；脚本存在不等于签名、公证已通过。

原生端到端验收必须分别覆盖 Intel / Apple Silicon，断网首次 venv、Finder 首启/重复双击/关闭/重开、仅回环监听与身份拒绝、合成 ZIP 导入和三阅读模式、主题与偏好持久化、Safari 文件选择与拖放、宽窄/亮暗/Reduced Motion、PG/应用退出恢复以及最终签名包断网首次启动。正式三期资料不能充当可删除的合成 fixture。每项需记录实际系统、Safari、Python、应用 hash、结果与证据；Windows 静态检查不能算作原生通过。

官方参考：[EDB 嵌入应用用二进制包](https://www.enterprisedb.com/download-postgresql-binaries)、[PostgreSQL macOS 支持](https://www.postgresql.org/download/macosx/)、[Psycopg 各安装实现与性能](https://www.psycopg.org/psycopg3/docs/basic/install.html)、[OpenSSL 当前各稳定系列](https://openssl-library.org/source/)。

## 共享配置协议修正与未来原生重建

默认 Finder 首启不传不存在的 `--machine` 路径。共享 MachineConfig 首先创建并核对本应用的 Library/Runtime 归属，再把实际选择持久化为上述私有 `machine-selected.json`；父进程将这个已存在文件传给 runtime 子进程。显式 `--machine` 或 `REFERENCE_LIBRARY_MACHINE_CONFIG` 指向不存在文件时直接拒绝，不退回默认资料库、不创建资料目录。重新打开保持同一个 LibraryUUID、PGDATA、Runtime 与 Cache；程序目录不写机器配置。所有程序、资料、状态与缓存路径及祖先目录必须是普通路径，不能包含 symlink/junction。

Windows 执行的 `test_machine_contract.py` 仅 mock Darwin 分支及真实 Mac 原生入口，核对这些配置、锁与子进程参数的纯合同。它不会启动 PG、Safari、Finder，不代表任何 Mac 原生通过。

`native-pg-version-lock.json` 是未来真实 Mac 的源编译目标：PostgreSQL/libpq 18.6、OpenSSL 4.0.3、ICU 78.3，分别构建 Intel 与 Apple Silicon，再依匹配的 libpq 构建 psycopg-c 3.3.6。`build_native_pg.sh` 只允许真实 Darwin 主机，在全新隔离输出目录编译这三份经校验源码；不使用 Homebrew、Docker、远端 CI，不修改正式资料库或现有官方 runtime。源归属和 SHA-256、Clang/SDK、native 依赖闭包、两架构功能及性能、签名/公证均须在执行时补充真实证据。版本 lock 已从 PostgreSQL 官方 checksum 与 OpenSSL/ICU 官方发布资产记录固定三个源码包 SHA-256；本轮没有下载或编译这些完整源码包。未来脚本逐个核验实际输入包后才提取和编译，不能把未取得的输入写成已构建。

现有 EDB 候选仍是 OpenSSL 3.5.9 / ICU 77.1 的 unsigned 功能实验候选；上述新目标与脚本不会把它伪装成“全部最新”或“原生已验收”。其他启用的非系统依赖必须另行锁定并验证兼容；系统库也须记录真实 Mac/SDK 来源。
