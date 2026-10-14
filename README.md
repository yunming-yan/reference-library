# 综合资料库 · Reference Library

用于在本机整理和阅读报告 ZIP 的资料库。默认 HTML 阅读，同时提供 Markdown 阅读、Markdown 源码、完整原件下载、标题与分类管理、回收站恢复和永久删除。

公开源码项目：[yunming-yan/reference-library](https://github.com/yunming-yan/reference-library)。本目录是清洁程序源码；完整离线平台包另行组装，当前没有公开二进制下载地址。

## 运行环境

最终用户提供标准 **CPython 3.14.8** 和本机浏览器。Windows 当前构建面向 x64；Mac Intel 与 Apple Silicon 分平台组装。用户不需要 Node.js、npm、Docker、Homebrew，也不需要自行安装 PostgreSQL 或 Python 数据库驱动。首启从运行包内的已校验文件准备私有环境，不在启动时联网下载或自动更新依赖。

| 组件 | 当前构建基线 |
| --- | --- |
| 主机 Python | CPython 3.14.8，标准构建 |
| 本机数据库 | PostgreSQL 18.6 |
| PostgreSQL Python 驱动 | Psycopg 3.3.6；Windows 当前 C 实现链接 libpq 18.6 |
| 连接池 | psycopg-pool 3.3.3 |
| 可重建渲染缓存 | APSW 3.54.0.0 / SQLite 3.54.0 |
| Markdown 解析器 | Python-Markdown 3.11.1，随源码和离线 wheel 提供 |

版本与来源记录见 `release/`、`runtime/postgresql/*/manifest.json` 和 `wheels/*/manifest.json`。这些记录不等于全部传递依赖都处于最新版本，也不等于二进制分发已经完成验收。

Windows 本机启动、独立存储、完整报告阅读及关闭重开已有实际验收；性能仍以对应机器、规模和负载的测量及限制为准。Mac 构建工具与分架构资源已准备，**当前 Mac 包尚未签名、公证，原生 Mac/Safari 验收仍待完成**；不能用 Windows 测试替代它们。详见 [Mac 构建与验收说明](docs/macos.md)。

## 数据与程序分开

- **ProgramRoot** 保存源码、页面、数据库工具和平台依赖，随程序版本分发。
- **LibraryRoot** 保存 PG 权威目录与完整原件：`catalog/pg18-main/`、`originals/`、`staging/`、`journals/`。
- **StateRoot** 保存本机配置、私有凭据、私有 Python 环境和运行日志。
- **CacheRoot** 保存可重建的完整 Markdown 渲染结果；SQLite 不保存唯一偏好、目录或回收站状态。
- 备份使用一致的 PG 逻辑快照、原件与校验清单，恢复到新目标；不将另一平台的 PGDATA 直接当作可移植备份。

启动器只管理本应用实例，只绑定 loopback；不注册系统服务、开机启动或远程接口。原件对象和 PG 目录通过持久库 ID 绑定，本机配置不能静默改指另一资料库。

## 导入与删除

标准报告布局为：

```text
md/原件/日期_期号_标题.md
md/附件/日期_期号/图示资源
htm/日期_期号_标题.html
```

`md/原文/` 也作为兼容别名接受。Markdown 中的 `../附件/日期_期号/资源.svg` 保持可解析。ZIP、Markdown、HTML 与附件按原字节保存；阅读器的安全处理不会重写原件。

导入在有界暂存后异步执行，提供进度与提交前取消。重复内容不会覆盖已有资料。标题、分类和阅读偏好由 PG 保存。

永久删除需要当前报告和当前回收站确认信息。后台先提交持久恢复记录，再清理原件；部分删除会生成仅含保留资料的剩余 ZIP。清理失败时资料包保持封锁，重启按日志恢复。当前提交阶段的导入取消返回 HTTP 409。只读验收模式由服务器拒绝业务写入。

## 源码与运行包

Git 源码导出包含必要程序、页面、schema、构建脚本、纯合成测试和许可证；排除真实报告、机器配置、凭据、截图、日志、虚拟环境与历史 Git 目录。大型 PostgreSQL 二进制、wheel 和构建归档放入相应平台运行包，不进入源码导出。

因此，源码 checkout 本身不是完整的离线双击运行包。运行包必须另外包含与平台匹配的 `runtime/`、`wheels/`、来源校验清单和第三方许可。Windows 包使用根目录的打开/关闭 VBS 与 `launch.py`；Mac 包由 `packaging/macos/` 构建工具组装。

公开副本可由开发者生成到一个新的空目录：

```text
python packaging/export_public.py --output <empty-output-directory> --evidence <private-evidence-directory>
```

导出器采用文件白名单，记录每个输出文件的 SHA-256、源清单与排除原因，拒绝私有路径、凭据和链接。它不初始化 Git，也不推送或发布。

不依赖数据库的合成边界测试可运行：

```text
python -m unittest discover -s tests -p test_backend_api.py
```

PG、缓存、性能和平台测试各有明确环境要求。合成数据测试、故障注入、受控重启、硬杀和原生平台验收分别记录，不能合并成一个全产品通过率。维护操作见 [维护工具说明](maintenance/README.md)。

## 许可

本项目原创代码采用 [MIT License](LICENSE)，版权署名为 `yunming-yan/reference-library`。第三方源码、驱动、数据库和运行库保留各自许可证；MIT 不重新许可这些组件。

第三方许可保存在 `licenses/`、`app/vendor/` 及数据库运行清单旁。PostgreSQL、Markdown、Psycopg、APSW/SQLite 等依各自条款使用。Microsoft 运行库的公开二进制再分发须另行确认许可权利；源码导出不含这些 DLL。

## Recovery, revisions and maintenance audit

Schema 4 keeps package admission identities separate from current survivor hashes. A partial permanent deletion can leave two independently imported packages with the same current bytes. Normal imports still reject duplicate current content under the PostgreSQL authority transaction. Importing the complete original ZIP after a partial deletion creates a new revision with new report identities; it preserves existing surviving reports and uses a durable pre-promotion identity plan for restart recovery.

Application shutdown pauses accepted imports while retaining their original upload and journal for restart. Startup recovery uses a fixed authority cutoff, two workers and at most eight queued/running Futures. New uploads keep the same bounded admission limit. Explicit cancellation and the current committing phase retain their separate rules.

Migration audits and snapshot inventories belong in the operator's external private output directory. New migrations retain no raw legacy catalog, audit or snapshot projection inside LibraryRoot. Existing prerelease projections require an externally pinned audit and exact-byte ownership registration before purge can remove them; unknown or changed projection files are refused and preserved. Logical backup and restore include the surviving originals and authority state.
