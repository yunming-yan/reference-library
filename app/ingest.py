"""Bounded ZIP ingestion with exact original-byte preservation."""
from __future__ import annotations
import hashlib, os, posixpath, re, stat, sys, threading, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
import markdown
from original_store import OriginalError, safe_name, atomic_json, hash_file, CHUNK

RENDERER_VERSION = "markdown-" + str(getattr(markdown, "__version__", "unknown")) + "-extra-toc-sane-v2"
MAX_ZIP = 100 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_FILES = 10000

class ContentScan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images=[]; self.resources=[]; self.tables=0; self.svgs=0; self.unsupported=set()
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag == "table": self.tables += 1
        if tag == "svg": self.svgs += 1
        if tag == "img":
            value=attrs.get("src", "").strip()
            if value: self.images.append(value); self.resources.append(value)
            # Empty image placeholders are omitted by the reader and do not invalidate a package.
            if attrs.get("srcset"): self.unsupported.add("srcset")
        if tag in ("image", "use"):
            value=attrs.get("href", attrs.get("xlink:href", "")).strip()
            if value: self.resources.append(value)
        if tag in ("script","iframe","object","embed","form","input","button","textarea","select","canvas","video","audio"):
            self.unsupported.add(tag)

def render_md(text):
    extensions=["markdown.extensions."+name for name in ("fenced_code","footnotes","attr_list","def_list","tables","abbr","md_in_html","toc","sane_lists")]
    return markdown.markdown(text, extensions=extensions,
                             extension_configs={"markdown.extensions.toc":{"permalink":False}})

def scan_html(text):
    scan=ContentScan(); scan.feed(text); return scan

def resolve_resource(ref, source, names):
    if not ref.strip(): return None, False
    parsed=urlsplit(ref)
    if parsed.scheme or parsed.netloc:
        return None, parsed.scheme.lower() != "data"
    if not parsed.path: return None, False
    decoded=unquote(parsed.path)
    if "\\" in decoded or decoded.startswith("/") or "\x00" in decoded:
        raise OriginalError("资源引用必须是包内相对路径：" + ref)
    normalized=posixpath.normpath(posixpath.join(str(PurePosixPath(source).parent),decoded))
    if normalized.startswith("../") or normalized == "..":
        raise OriginalError("资源引用越出 ZIP 根目录：" + ref)
    item=names.get(normalized.casefold())
    if not item or item[1].is_dir():
        raise OriginalError("正文引用的图示资源缺失：" + ref)
    return item[0], False

def checkpoint(cancelled, progress, phase, value):
    if cancelled and cancelled(): raise OriginalError("导入已取消。", 409)
    if progress: progress(phase, value)

def prepare_zip(vault, stage_id, object_id, filename, cancelled=None, progress=None, *, extract_workers=4):
    stage=vault.stage_path(stage_id)
    source=vault.checked(stage/"source.zip", vault.staging)
    if not source.is_file() or source.stat().st_size > MAX_ZIP:
        raise OriginalError("ZIP 为空或超过 100 MB。", 413)
    digest=hash_file(source)
    checkpoint(cancelled,progress,"validating",0.05)
    try:
        with zipfile.ZipFile(source) as archive:
            infos=archive.infolist()
            if len(infos)>MAX_FILES: raise OriginalError("ZIP 成员数超过 10,000。",413)
            names={}; total=0
            for info in infos:
                name=safe_name(info.orig_filename)
                mode=info.external_attr>>16
                if stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)):
                    raise OriginalError("ZIP 中包含链接或特殊文件。")
                key=name.casefold()
                if key in names: raise OriginalError("ZIP 中存在重复或大小写冲突路径："+name)
                names[key]=(name,info); total+=info.file_size
                if info.flag_bits & 1: raise OriginalError("请先解密 ZIP 再导入。")
                if info.file_size>MAX_EXPANDED or total>MAX_EXPANDED:
                    raise OriginalError("ZIP 解压总大小超过 512 MB。",413)
            for name, info in names.values():
                for parent in PurePosixPath(name).parents:
                    item=names.get(str(parent).casefold())
                    if item and not item[1].is_dir(): raise OriginalError("ZIP 文件和目录路径冲突："+name)
            md_names=[n for n,i in names.values() if not i.is_dir() and n.casefold().startswith(("md/原件/", "md/原文/")) and n.lower().endswith(".md")]
            if not md_names: raise OriginalError("未发现 md/原件/ 中的 Markdown 全文（也支持 md/原文/）。")
            if type(extract_workers) is not int or not 1<=extract_workers<=4:
                raise OriginalError("Extraction workers must be between 1 and 4.")
            canonical=hashlib.sha256(); members=[]; processed=0
            parse_cache={}; cache_size=0; stop=threading.Event()
            failure=[None]; failure_lock=threading.Lock()
            pairs=sorted(names.values(),key=lambda pair:pair[0])
            targets={}
            for name,info in pairs:
                target=vault.checked(stage/"files"/Path(*PurePosixPath(name).parts),vault.staging)
                if info.is_dir(): target.mkdir(parents=True,exist_ok=True)
                else: target.parent.mkdir(parents=True,exist_ok=True)
                targets[name]=target
            def extract(pair):
                name,info=pair
                try:
                    if stop.is_set(): raise OriginalError("Extraction stopped after a validation failure.")
                    if cancelled and cancelled(): raise OriginalError("导入已取消。",409)
                    target=vault.checked(targets[name],vault.staging)
                    if info.is_dir():return None
                    small=info.file_size<=32*1024 and PurePosixPath(name).suffix.lower() in (".md",".html",".htm",".svg")
                    buffered=bytearray() if small else None
                    file_hash=hashlib.sha256();size=0
                    with archive.open(info) as inp,target.open("wb") as out:
                        while chunk:=inp.read(CHUNK):
                            if stop.is_set(): raise OriginalError("Extraction stopped after a validation failure.")
                            if cancelled and cancelled(): raise OriginalError("导入已取消。",409)
                            size+=len(chunk)
                            if size>info.file_size or size>MAX_EXPANDED:raise OriginalError("ZIP 解压大小与清单不一致。")
                            out.write(chunk);file_hash.update(chunk)
                            if buffered is not None:buffered.extend(chunk)
                        if size!=info.file_size:raise OriginalError("ZIP 成员长度不一致。")
                        out.flush();os.fsync(out.fileno())
                    return ({"path":name,"size":size,"sha256":file_hash.hexdigest()},file_hash.digest(),bytes(buffered) if buffered is not None else None)
                except Exception as exc:
                    with failure_lock:
                        if failure[0] is None:failure[0]=exc
                    stop.set();raise
            pool=ThreadPoolExecutor(max_workers=extract_workers,thread_name_prefix="zip-member") if extract_workers>1 else None
            try:
                results=pool.map(extract,pairs,buffersize=extract_workers*2) if pool else map(extract,pairs)
                for result in results:
                    checkpoint(cancelled,progress,"extracting",0.1+0.5*processed/max(total,1))
                    if result is None:continue
                    member,digest_bytes,buffered=result
                    members.append(member);processed+=member["size"]
                    canonical.update(member["path"].encode("utf-8"));canonical.update(digest_bytes)
                    if buffered is not None and cache_size+len(buffered)<=16*1024*1024:
                        parse_cache[member["path"]]=buffered;cache_size+=len(buffered)
            except Exception:
                stop.set()
                if failure[0] is not None:raise failure[0]
                raise
            finally:
                # A successful package cannot advance until every file has passed CRC
                # and completed its own durable flush; failed workers finish before cleanup.
                if pool:pool.shutdown(wait=True,cancel_futures=True)
            def text_at(name):
                raw=parse_cache.get(name)
                if raw is None:return (stage/"files"/Path(*PurePosixPath(name).parts)).read_text(encoding="utf-8-sig")
                # Preserve read_text(newline=None) semantics; original bytes are untouched.
                return raw.decode("utf-8-sig").replace("\r\n","\n").replace("\r","\n")
            now=datetime.now(timezone.utc).isoformat()
            records=[]; documents={}
            for idx,name in enumerate(sorted(md_names)):
                checkpoint(cancelled,progress,"parsing",0.62+0.28*idx/max(len(md_names),1))
                stem=PurePosixPath(name).stem
                html_candidates=["htm/"+stem+".html","html/"+stem+".html"]
                html_item=next((names.get(n.casefold()) for n in html_candidates if names.get(n.casefold())),None)
                if not html_item or html_item[1].is_dir(): raise OriginalError("缺少配套 HTML 文件：htm/"+stem+".html")
                html_name=html_item[0]
                md=text_at(name)
                html=text_at(html_name)
                if not md.strip(): raise OriginalError("Markdown 全文为空："+name)
                md_scan=scan_html(render_md(md)); html_scan=scan_html(html)
                attachments=[]; warnings=[]
                for scan, origin in ((md_scan,name),(html_scan,html_name)):
                    for ref in scan.resources:
                        resource,external=resolve_resource(ref,origin,names)
                        if resource: attachments.append(resource)
                        if external: warnings.append("存在外部图示；阅读器只使用包内资源，请将图示随 ZIP 一起提供。")
                pending=list(dict.fromkeys(attachments)); visited=set()
                while pending:
                    resource=pending.pop()
                    if resource in visited: continue
                    visited.add(resource)
                    if resource.lower().endswith(".svg"):
                        svg=scan_html(text_at(resource))
                        if svg.unsupported: warnings.append("SVG 包含动态内容，阅读器只显示其静态部分。")
                        for ref in svg.resources:
                            nested,external=resolve_resource(ref,resource,names)
                            if nested: attachments.append(nested); pending.append(nested)
                            if external: warnings.append("SVG 引用了外部资源；请提供自包含的静态 SVG。")
                if md_scan.unsupported: warnings.append("Markdown 包含动态或交互内容，阅读器会安全地展示静态内容，原件完整保留。")
                # Original HTML navigation controls are common scaffolding, not missing report content.
                if html_scan.tables>md_scan.tables or len(html_scan.images)+html_scan.svgs>len(md_scan.images)+md_scan.svgs:
                    warnings.append("HTML 图表内容多于 Markdown；切换阅读模式时请核对，两个原件均完整保留。")
                date=re.search(r"\d{4}-\d{2}-\d{2}",stem)
                tail=stem[date.end():] if date else stem
                issue=re.search(r"(?:第\s*)?(\d+)\s*期",tail) or re.search(r"\d+",tail)
                title=re.search(r"^#\s+(.+)$",md,re.M)
                rid=digest[:24]+"-"+str(idx)
                record={"id":rid,"package":digest,"md":name,"html":html_name,
                        "title":title.group(1).strip() if title else stem,"date":date.group(0) if date else "日期未标注",
                        "issue":(issue.group(1) if issue.lastindex else issue.group(0)).zfill(2) if issue else "—",
                        "characters":len(re.sub(r"\s","",md)),"attachments":list(dict.fromkeys(attachments)),
                        "tableCount":md_scan.tables,"imageCount":len(md_scan.images),"warnings":list(dict.fromkeys(warnings)),
                        "importedAt":now,"sourceName":Path(filename).name}
                records.append(record); documents[rid]=md
            manifest={"version":2,"package":digest,"sha256":digest,"currentSha256":digest,"objectId":object_id,
                      "contentSha256":canonical.hexdigest(),"fileCount":len(members),"expandedBytes":total,
                      "importedAt":now,"sourceName":Path(filename).name,"reports":records,"files":members}
            atomic_json(stage/"metadata.json",manifest)
            checkpoint(cancelled,progress,"ready",0.92)
            return manifest, documents
    except (zipfile.BadZipFile,RuntimeError,UnicodeError,NotImplementedError) as exc:
        raise OriginalError("ZIP 校验失败；请确认文件完整、成员采用 UTF-8 编码。") from exc

def documents_at(vault, manifest):
    return {r["id"]:vault.member_path(manifest,r["md"]).read_text(encoding="utf-8-sig")
            for r in manifest["reports"]}

