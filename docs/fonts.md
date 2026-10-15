# 内置阅读字体与可复现构建

完整离线应用内置四个字体家族、六份未经修改的字体文件。浏览器只从本地应用读取所选字体；应用启动和阅读不会下载字体，也不安装系统字体。`system` 选项继续使用本机现有字体，不分发 Apple 字体。

| 家族 | 应用内资源 | 公开应用源码 |
|---|---|---|
| MiSans | 原版 Regular / Bold WOFF2 | 不提供原始字体文件；保留官方获取脚本、完整许可、署名和校验清单 |
| Noto Sans SC | 原版可变 TTF，100–900 字重 | 保留四款 OFL 资源中的这一份原始文件 |
| 霞鹜文楷屏幕阅读版 | 原版单字重 TTF | 原始文件及完整 OFL / 版权；粗体可由浏览器合成 |
| 寒蝉全圆体 ChillRoundF | 原版 Regular / Bold TTF | 两份原始文件及完整 OFL / 版权 |

MiSans 使用其专有许可；另外三个家族使用 SIL Open Font License 1.1。项目采用保守的分发政策：MiSans 原始字节仅嵌在完整应用成品中，不发布独立字体仓库、字体包或原始字体链接。参见[官方 FAQ](https://hyperos.mi.com/font/zh/faq/)及随源码保留的[原始许可 PDF](../licenses/fonts/misans/LICENSE.pdf)。软件内署名与完整通知见各家族 `NOTICE.txt`；字体许可不改变本应用源代码的 MIT 许可。

两份相同的资源清单位于 `release/bundled-fonts.json` 和 `app/web/fonts/manifest.json`，精确 SHA256 均为 `bcb501c6c3d6b708658b7756c6db47e6524023f11903c4c55ad9076cb02be00a`。它们记录官方来源、版本、原始字重、CSS 映射、字体轴、许可证及每个文件的字节数和 SHA256。MiSans 文件名 Regular/Bold 对应原字体内部字重 330/630，CSS 映射为 400/700；没有重命名字体内部信息或改写字节。

## 从公开源码准备完整应用输入

以下命令在源码根目录执行，使用标准 Python 3.14。准备脚本仅在开发构建阶段运行，不引入第三方 Python 包。已下载成品的用户不需要执行这些命令。

```console
git clone https://github.com/yunming-yan/reference-library.git
cd reference-library
python -X utf8 -B packaging/prepare_misans.py
python -X utf8 -B packaging/prepare_misans.py --verify-only
python -X utf8 -B packaging/export_public.py --source . --output ../reference-library-build/application-source --evidence ../reference-library-build/application-export --distribution application
```

`prepare_misans.py` 只访问清单固定的官方 Xiaomi ZIP URL，使用严格 HTTP 206 Range 读取两份成员；它校验中央目录中的成员名、大小、CRC、解压边界及最终 SHA256。官方 ZIP 的 local header 使用 data descriptor，CRC/大小可能为零，因此以中央目录为准。已存在且完全一致的文件直接复用；不同内容会被保留并报错。新文件通过同目录临时文件及原子、不覆盖的安装完成。脚本不生成独立字体 ZIP。

这条实际准备链已在缺少 MiSans 的独立目录验证：先从公开许可与四份 OFL 资源出发，从官方 ZIP 取回两份原始 WOFF2；第二次 `--verify-only` 未联网且所有 26 个字体、许可、通知、清单资源的 SHA 完全一致。Windows 实测不等于 Mac 原生应用验收；该脚本只使用跨平台 Python 标准库。

`--distribution application` 是完整应用构建输入，强制六份字体全部存在且正确。默认的 `--distribution source` 是供公开仓库使用的独立导出，刻意排除 MiSans 两份原始字体；两种导出使用不同输出目录，不能拿公开导出冒充完整应用输入，也不能将 application 导出直接推入公开仓库。

```console
python -X utf8 -B packaging/export_public.py --source . --output ../reference-library-build/public-source --evidence ../reference-library-build/public-export
```

导出器只接受固定清单中的字体/许可资产，未开放 `.ttf`、`.woff2` 或 `.pdf` 的通用规则。每份字体小于 100 MB；普通源文本仍使用 16 MiB 上限。未经批准的二进制、原生运行库、数据库、报告和私有配置继续排除。两种 manifest 都明确记录实际字体资产及分发目的。

## 使用原 Windows 组包器

公开的 `packaging/build_windows_release.py` 保留之前组包器的算法和参数，只把六个安全扫描标记改为分段构造，使公开脚本能通过自身扫描；全部编译后的检测 pattern 与 flags 已逐项验证相同。公开版本 SHA256 为 `2de9459c1d10fd963bad1a3884601a87031a26ff68c085df6b9658cbf7e5d08e`，原始私有组包器 SHA256 仍为 `19bce76b1b6193b7654b6ad1b5f4cc816ac0a6acebec7a716dad9cd0677dab20`。它直接读取上一步的 application 导出，所以六份字体不是私有脚本才能补入的隐藏依赖。

Windows 原生运行库和 wheel 仍按 [Windows 运行说明](../release/WINDOWS_RUNTIME.md)及[原生构建说明](../release/WINDOWS_NATIVE_BUILD.md)准备；它们不随 Git 源码发布。本轮字体更新不重新编译它们。下面的冻结示例只接受已经实际验证的原有运行库树 `eca97ffdfe44365b105ea50bcc7cac014f8647626659dea260f3faf5d2ecf50f` 及 wheel 树 `af97d98735c7a1d844f518ceb7acfbad4842167a815980f9aaa2f0b2a52a1366`；不相同就停止，不能把任意新运行库标为已验证。

```console
python -X utf8 -B -c "import importlib.util,json;from pathlib import Path;s=importlib.util.spec_from_file_location('release_builder',Path('packaging/build_windows_release.py'));b=importlib.util.module_from_spec(s);s.loader.exec_module(b);p=Path('../reference-library-build');e=p/'application-export/export-manifest.json';m=json.loads(e.read_text('utf-8'));rt=Path('runtime/postgresql/windows-x64');wh=Path('wheels/windows-x64-cp314');r=b.tree_hash(b.walk(rt));w=b.tree_hash(b.walk(wh));assert m['status']=='PASS' and m['distributionTarget']=='application';assert r=='eca97ffdfe44365b105ea50bcc7cac014f8647626659dea260f3faf5d2ecf50f' and w=='af97d98735c7a1d844f518ceb7acfbad4842167a815980f9aaa2f0b2a52a1366';f={'format':1,'status':'READY','scope':'windows-offline-release','expectedSchemaVersion':4,'runtimeValidated':True,'sourceFrozen':True,'sourceTreeSha256':m['treeSha256'],'exportManifestSha256':b.sha_file(e),'runtimeTreeSha256':r,'wheelTreeSha256':w,'requiredSourcePaths':[],'requiredRuntimePaths':[],'forbiddenTokens':[]};(p/'windows-input-freeze.json').open('x',encoding='utf-8').write(json.dumps(f))"
python -X utf8 -B packaging/build_windows_release.py --source ../reference-library-build/application-source --export-manifest ../reference-library-build/application-export/export-manifest.json --runtime-root runtime/postgresql/windows-x64 --wheel-root wheels/windows-x64-cp314 --freeze-manifest ../reference-library-build/windows-input-freeze.json --output ../reference-library-build/windows-offline
```

组包器校验全部输入与清单、每一份原始字体字节、展开 wheel 的隐私检查、输出路径、完整 Program inventory、ZIP CRC 及每个 ZIP 成员 SHA。示例输出必须是新目录；失败产物保留，不能覆盖原包。源码不含桌面 ICO 二进制：应用功能及六字体不依赖 ICO；本机发布流程额外使用已经验证的原始 logo.ico 作为快捷方式图标，并将其计入成品清单。

字体更新只涉及阅读控件和这些应用资源。Mac 最新原生依赖、Finder/Safari 实测、签名/公证及旧并发性能缺口仍属于各自尚未完成的验收范围。