# Windows 离线运行时

2026-10-14 官方稳定版本复核锁定：PostgreSQL 18.6、OpenSSL 4.0.3、ICU 78.3、zlib 1.3.2、LZ4 1.10.0、Zstandard 1.5.7、IANA 时区 2026e、Microsoft VC Runtime 14.51.36247.0。PostgreSQL 使用官方源码在隔离目录原生构建，ICU 使用官方 x64 SDK，其他列出的原生库使用官方源码构建；没有把 EDB 旧 DLL 的版本写成最新版本。

Python 离线依赖为 psycopg / psycopg-c 3.3.6、psycopg-pool 3.3.3、APSW 3.54.0.0（内含 SQLite 3.54.0）、tzdata 2026.5、typing-extensions 4.16.0；Markdown 3.11.1 的源文件、许可证和离线 wheel 随应用保存。CPython 3.14.8 x64、Windows 系统 DLL/UCRT 和已安装浏览器由本机提供。固定的发布版本不会在日常启动时自动联网更新。这里的“最新”指这次核实并锁定的应用依赖，不声明 Windows 或 Python 内部所有组件均由本程序更新。

双击“打开综合资料库.vbs”，启动器探测本机标准 Python，在 StateRoot 建立与 Python 版本、程序位置、依赖锁及私有原生加载器源码绑定的 venv，并通过 `--no-index --require-hashes` 从本地 wheels 安装。首次校验 PostgreSQL 逐文件 SHA-256，随后仅在清单指纹及文件属性均未变化时复用验证缓存。程序位置或 Python 变化会重建本机 venv，发布包不搬运另一台机器的 venv。无需用户安装 Node、Docker、pip 或编译器，也不捆绑 Python 或浏览器。

打开和关闭 VBS 都接受可选的一个绝对、已存在的私有 machine JSON 路径，等价于 `launch.py start/stop --machine <path>`。无参数时使用标准首次配置。快捷方式可把持久的 machine JSON 路径作为 VBS 第二个参数传给 wscript。显式指定的配置不存在时拒绝启动，不会另开默认空库。

ProgramRoot 是解压位置；LibraryRoot、StateRoot、CacheRoot 必须是互不包含的普通绝对目录，并拒绝祖先目录中的符号链接或 junction。默认目录为本机 LocalAppData/ReferenceLibrary 下的 library、runtime、cache。**PGDATA 始终属于持久 LibraryRoot/catalog/pg18-main**；清缓存或重建 venv 不删除目录库。私有机器配置可指定 `dataRoot`、`privateStateRoot`、`cacheRoot`、`pgDataDir`、`appPort`、`pgPort`、`forbiddenRoots`。生产 pgDataDir 必须在 LibraryRoot 内。测试专用 protected-QA override 不作为生产部署方式。

目录身份使用同一 libraryId 绑定 Library marker、PG runtime identity 及 PostgreSQL catalog identity。仅空的新目录/新 PGDATA 可以初始化；已有内容无有效归属标记时拒绝覆盖，必须显式迁移。机器路径、账号与随机 SCRAM 凭据仅保存在 ACL 受限的 StateRoot，公开源码和发布模板不包含这些值。应用账号不是超级用户；PostgreSQL 与 API 仅监听 127.0.0.1。默认 PG 不启用 TLS，本次仍验证了随包 OpenSSL 的 TLS 1.3 能力。

重复打开复用同一应用实例。浏览器关闭后服务继续运行；“关闭综合资料库.vbs”让 API 完成受控后台收尾，再关闭 API 和本应用 PostgreSQL。启动器使用私有无代理 HTTP opener，用户的 HTTP_PROXY/HTTPS_PROXY 不会改道本机 health/shutdown。停库前检查 PGDATA、可执行路径、Windows 进程出生时间、实际连接的数据库目录及 backend 父进程，拒绝同程序另库或被重用 PID。ProgramRoot 日常不写字节码和状态文件。

原生构建启用 ICU、TLS/SCRAM、pg_trgm 及 gzip/LZ4/Zstandard 备份压缩，保留标准服务端、libpq、initdb、pg_ctl、pg_dump、pg_restore。未使用的 NLS、XML/XSLT、OAuth/libcurl、外部 Perl/Python/Tcl 语言、LLVM、LDAP/GSSAPI、readline 明确禁用；它们不属于本应用功能。备份与迁移使用逻辑工具，不复制运行中的 PGDATA。

逐文件清单为 `runtime/postgresql/windows-x64/manifest.json`；来源、版本和许可证表为 `release/windows-runtime-dependencies.json`；官方来源锁与构建方法为 `release/windows-native-build-lock.json` 和 `release/WINDOWS_NATIVE_BUILD.md`。源归档中的 PostgreSQL tzdata 2026c 按上游文档更新为 2026e，时区引擎未改动。PostgreSQL C/UTF8 核心上游 231 项、pg_trgm 4 项、pgcrypto 25 项、isolation 122 项通过；OpenSSL 全默认上游 4,135 断言通过，平台/禁用特性按上游跳过。保留第一次宿主中文 locale 导致的 ICU/cascade 差异，不修改 expected 伪造通过。

运行时 `licenses/` 与应用 `licenses/` 保留 PostgreSQL、Unicode、OpenSSL、压缩库、驱动及 Microsoft 原始许可证和 notices。源码发布不包含数据库二进制、wheels、CRT、配置、PGDATA、venv、原件或私有日志。**公开二进制再分发仍须满足 Microsoft Runtime License 的授权和分发条件**；已有 BuildTools 不能单独证明 Visual Studio Products 许可。私有本机包已经完成验证，该验证不等于确认公开 CRT 再分发权利。构建锁明确区分发布者 checksum 与仅本地计算的 hash，不把校验值称为签名。

PostgreSQL 启动最多等待 5 分钟，正常就绪立即返回，并非固定等待；恢复超过上限时先查看私有日志并稍后重试，严格所有权检查不会跳过。

私有 venv 的原生扩展与应用 DLL 加载使用 Windows extended-length 绝对路径。加载器源码 SHA-256 参与环境指纹，修改加载器会创建新私有环境并保留旧环境；复用时核对加载器及 .pth 字节。此处理不改主机 Python、注册表或全局 PATH，也不宣称所有 Windows shell/数据库路径都无长度限制。
