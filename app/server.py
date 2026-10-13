"""Reference Library local HTTP API. PostgreSQL owns catalogue and state."""
from __future__ import annotations
import argparse, concurrent.futures, json, mimetypes, os, re, sys, threading, time, uuid, weakref
from contextlib import contextmanager
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlsplit
from original_store import OriginalStore, OriginalError, atomic_json, hash_file, safe_name, CHUNK
from ingest import MAX_ZIP, prepare_zip, documents_at, render_md, scan_html, RENDERER_VERSION

ROOT=Path(__file__).resolve().parent
PATCH_VERSION="2.0.0-dev"
MIME={".svg":"image/svg+xml",".md":"text/plain; charset=utf-8",".html":"text/html; charset=utf-8",
      ".css":"text/css; charset=utf-8",".js":"text/javascript; charset=utf-8"}
CONTENT_CSP="sandbox; default-src 'none'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
APP_CSP="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'none'"
RID=re.compile(r"[a-f0-9]{24}-\d+")
JOBID=re.compile(r"[a-f0-9-]{20,80}")

class ArchiveError(OriginalError):
    def __init__(self,message,status=400,existing=None,code=None):
        super().__init__(message,status); self.existing=existing; self.code=code

def default_reading():
    return {"library":True,"toc":True,"fontSize":18,"lineHeight":1.8,"width":"balanced","focus":False,
            "focusRestore":{"library":True,"toc":True}}

def validate_reading(value):
    if not isinstance(value,dict) or not set(value).issubset(default_reading()):
        raise ArchiveError("阅读设置包含无效字段。")
    for key,item in value.items():
        if key in ("library","toc","focus") and type(item) is not bool: raise ArchiveError("阅读状态必须是布尔值。")
        if key=="fontSize" and (type(item) is not int or item not in (16,17,18,20,22)): raise ArchiveError("阅读字号无效。")
        if key=="lineHeight" and (type(item) not in (int,float) or item not in (1.6,1.8,2)): raise ArchiveError("Invalid reading line height.")
        if key=="width" and item not in ("narrow","balanced","wide"): raise ArchiveError("阅读宽度无效。")
        if key=="focusRestore" and (not isinstance(item,dict) or not set(item).issubset({"library","toc"}) or any(type(v) is not bool for v in item.values())):
            raise ArchiveError("专注模式恢复状态无效。")
    return value

def normalized_preferences(saved):
    result={"theme":"light","reading":default_reading()}
    if not isinstance(saved,dict): return result
    if saved.get("theme") in ("light","dark"): result["theme"]=saved["theme"]
    if isinstance(saved.get("reading"),dict):
        for key,value in saved["reading"].items():
            try: validate_reading({key:value})
            except ArchiveError: continue
            if key=="focusRestore": result["reading"][key].update(value)
            else: result["reading"][key]=value
    return result

def public_job(job):
    if not job: raise ArchiveError("任务不存在。",404)
    return {key:job[key] for key in ("id","kind","state","phase","progress","message","error","result",
                                     "cancelRequested","createdAt","updatedAt") if key in job}

class Store:
    def __init__(self,root,catalog=None,cache_root=None,read_only=None):
        self._closing=threading.Event(); self._close_guard=threading.Lock()
        self._executor=None; self._lease=None; self._recovery_thread=None
        self.catalog=None; self.cache=None; self._resources_closed=False
        self._recovery_error=None
        try:
            self._initialize(root,catalog,cache_root,read_only)
        except BaseException:
            try: self.close()
            except Exception: pass
            raise

    def _initialize(self,root,catalog=None,cache_root=None,read_only=None):
        self.read_only=os.environ.get("REFERENCE_LIBRARY_READ_ONLY")=="1" if read_only is None else bool(read_only)
        connection=None; expected_library_id=None
        if catalog is None:
            from pg_catalog import Catalog
            config=os.environ.get("REFERENCE_LIBRARY_PG_CONFIG")
            if config:
                raw_config=json.loads(Path(config).read_text(encoding="utf-8-sig"))
                expected_library_id=raw_config.get("libraryId")
                connection={k:raw_config[k] for k in ("host","port","dbname","user","password") if k in raw_config}
            else: connection=os.environ.get("REFERENCE_LIBRARY_DSN") or os.environ.get("REFERENCE_LIBRARY_PG_DSN")
        self.vault=OriginalStore(root,library_id=expected_library_id); self.root=self.vault.root
        self.staging=self.vault.staging
        if catalog is None: catalog=Catalog(connection,initialize=not self.read_only)
        self.catalog=catalog
        self.library_id=self.catalog.bind_library(self.vault.library_id,initialize_if_empty=not self.read_only,read_only=self.read_only)
        requested_instance=os.environ.get("REFERENCE_LIBRARY_INSTANCE_ID")
        try: self.instance_id=str(uuid.UUID(requested_instance)) if requested_instance else str(uuid.uuid4())
        except (ValueError,TypeError,AttributeError) as exc: raise ArchiveError("Application instance identity is invalid.",409) from exc
        self.cache=None
        cache_root=cache_root or os.environ.get("REFERENCE_LIBRARY_CACHE")
        if cache_root:
            from cache_sqlite import RenderedCache
            self.cache=RenderedCache(cache_root)
        self._locks=weakref.WeakValueDictionary(); self._locks_guard=threading.Lock()
        self._mutations=threading.RLock(); self._closing=threading.Event()
        self._slots=threading.BoundedSemaphore(8)
        self._executor=concurrent.futures.ThreadPoolExecutor(max_workers=2,thread_name_prefix="library-job")
        self._futures=set(); self._job_futures={}; self._futures_guard=threading.Lock()
        self._lease=self._acquire_lease()
        if not self.read_only: self.recover()

    def require_writable(self):
        if self.read_only: raise ArchiveError("此资料库正在进行只读验收，暂不接受修改。",403,code="read_only")

    def _acquire_lease(self):
        path=self.vault.checked(self.root/".runtime.lock")
        stream=path.open("a+b")
        if path.stat().st_size == 0: stream.write(b"1"); stream.flush()
        stream.seek(0)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close(); raise ArchiveError("该资料库已由另一应用进程使用。",409)
        return stream

    @contextmanager
    def library_lock(self):
        with self.catalog.library_lock(shared=True): yield

    def package_lock(self,package):
        with self._locks_guard: return self._locks.setdefault(package,threading.RLock())

    @staticmethod
    def validate_rid(rid):
        if not RID.fullmatch(rid): raise ArchiveError("报告标识无效。",404)

    def find(self,rid):
        self.validate_rid(rid)
        return self.catalog.get_report(rid)

    def package(self,package,reference_only=False):
        if not re.fullmatch(r"[a-f0-9]{64}",package): raise ArchiveError("资料包标识无效。",404)
        manifest=self.catalog.get_package_ref(package) if reference_only else self.catalog.get_package(package)
        if not manifest: raise ArchiveError("资料包不存在。",404)
        if manifest.get("blocked"): raise ArchiveError("资料包的持久操作尚未完成，已暂时封锁。",503,code="package_blocked")
        return manifest

    @contextmanager
    def readable(self,rid):
        record=self.find(rid)
        if not record: raise ArchiveError("报告不存在。",404)
        with self.library_lock(), self.package_lock(record["package"]):
            current=self.find(rid)
            if not current or current.get("importedAt")!=record.get("importedAt"):
                raise ArchiveError("报告状态已变化，请刷新资料库。",409)
            manifest=self.package(record["package"],reference_only=True)
            members=[self.catalog.get_member(record["package"],current[k]) for k in ("md","html")]
            if any(member is None for member in members): raise ArchiveError("Original member mapping is missing.",503)
            manifest["files"]=members
            yield current,manifest

    def list(self,query="",category="",trash=False,offset=0,limit=50,cursor=None,generation=None):
        result=self.catalog.list_reports(query,category,trash,offset,limit,cursor,generation)
        result["settings"]=normalized_preferences(self.catalog.preferences())
        result["storage"]=str(self.vault.root)
        return result

    def reader(self,rid):
        with self.readable(rid) as (record,manifest):
            md=self.vault.member_path(manifest,record["md"]).read_bytes().decode("utf-8-sig")
            original=self.vault.member_path(manifest,record["html"]).read_bytes().decode("utf-8-sig")
            prefix="/reports/"+record["package"]+"/files/"
            content=None
            md_hash=next(member["sha256"] for member in manifest["files"] if member["path"]==record["md"])
            key=(rid,record.get("reportGeneration",record.get("importedAt")),md_hash,RENDERER_VERSION)
            if self.cache:
                try: content=self.cache.get(*key)
                except Exception: pass
            if content is None:
                content=render_md(md)
                if self.cache:
                    try: self.cache.put(*key,content)
                    except Exception: pass
            return {"report":record,"content":content,"base":prefix+quote(str(PurePosixPath(record["md"]).parent))+"/",
                    "htmlSource":original,"htmlBase":prefix+quote(str(PurePosixPath(record["html"]).parent))+"/",
                    "markdownSource":md,"generation":record.get("generation",manifest.get("generation"))}

    def update_metadata(self,rid,changes):
        self.require_writable()
        self.validate_rid(rid)
        if not isinstance(changes,dict) or not changes or not set(changes).issubset({"title","category","generation"}):
            raise ArchiveError("只可修改报告标题和分类。")
        cleaned={}
        for key,limit in (("title",200),("category",80)):
            if key not in changes: continue
            value=changes[key]
            if not isinstance(value,str): raise ArchiveError("标题和分类必须是文本。")
            value=value.strip()
            if (key=="title" and not value) or len(value)>limit or any(ord(c)<32 for c in value):
                raise ArchiveError("标题或分类含无效字符。")
            cleaned[key]=value
        if not cleaned: raise ArchiveError("未提交标题或分类。")
        with self.library_lock(),self._mutations:
            return self.catalog.update_metadata(rid,cleaned,generation=changes.get("generation"))

    def set_trash(self,rid,changes):
        self.require_writable()
        self.validate_rid(rid)
        if not isinstance(changes,dict) or not set(changes).issubset({"restore","generation"}) or type(changes.get("restore",False)) is not bool:
            raise ArchiveError("回收站操作必须提交 restore 布尔值。")
        with self.library_lock(),self._mutations:
            return self.catalog.set_trash(rid,changes.get("restore",False),generation=changes.get("generation"))

    def preferences(self): return normalized_preferences(self.catalog.preferences())

    def update_preferences(self,changes):
        self.require_writable()
        if not isinstance(changes,dict) or not changes or not set(changes).issubset({"theme","reading"}):
            raise ArchiveError("偏好设置只接受 theme 和 reading。")
        if "theme" in changes and changes["theme"] not in ("light","dark"): raise ArchiveError("主题无效。")
        if "reading" in changes: validate_reading(changes["reading"])
        with self.library_lock(): return self.catalog.update_preferences(changes)

    def _submit(self,callback,*args,slot_reserved=False,job_id=None):
        if not slot_reserved and not self._slots.acquire(blocking=False):
            raise ArchiveError("后台任务已满，请稍后重试。",429,code="job_capacity")
        try: future=self._executor.submit(callback,*args)
        except Exception:
            self._slots.release(); raise
        with self._futures_guard:
            self._futures.add(future)
            if job_id: self._job_futures[job_id]=future
        def done(value):
            self._slots.release()
            with self._futures_guard:
                self._futures.discard(value)
                if job_id and self._job_futures.get(job_id) is value: self._job_futures.pop(job_id,None)
        future.add_done_callback(done)

    def accept_upload(self,inp,length,filename):
        self.require_writable()
        if self._closing.is_set(): raise ArchiveError("应用正在关闭。",503)
        if not self._slots.acquire(blocking=False): raise ArchiveError("后台任务已满，请稍后重试。",429)
        stage_id=uuid.uuid4().hex; object_id=uuid.uuid4().hex
        job_id=uuid.uuid4().hex; operation=None
        try:
            with self.library_lock():
                stage=self.vault.new_stage(stage_id)
                with (stage/"source.zip").open("xb") as out:
                    remaining=length
                    while remaining:
                        chunk=inp.read(min(CHUNK,remaining))
                        if not chunk: raise ArchiveError("上传未完成，请重新导入。")
                        out.write(chunk); remaining-=len(chunk)
                    out.flush(); os.fsync(out.fileno())
                payload={"stageId":stage_id,"objectId":object_id,"sourceName":Path(filename).name,"jobId":job_id}
                job=self.catalog.create_job("import",payload,job_id=job_id)
                operation=self.catalog.begin_import(job_id,payload)
                self.vault.write_journal(operation)
            self._submit(self._run_import,operation,slot_reserved=True,job_id=job_id)
            return {"job":public_job(job),"jobId":job_id,"message":"文件已接收，正在后台校验和导入。"}
        except Exception:
            if operation:
                # The committed intent remains recoverable; never erase its upload.
                try: self.catalog.update_job(job_id,state="failed",message="导入暂存已保留，重启将恢复。")
                except Exception: pass
            else: self.vault.discard_stage(stage_id)
            self._slots.release()
            raise

    def _cancel_import_future(self,job_id):
        with self._futures_guard: future=self._job_futures.get(job_id)
        if future: future.cancel()

    def _finish_import_cancellation(self,operation):
        job_id=operation.get("jobId") or operation["payload"]["jobId"]
        with self.package_lock("cancel:"+job_id),self.library_lock():
            current=self.catalog.get_import_operation(job_id)
            if not current or current["id"]!=operation["id"]:
                raise ArchiveError("取消恢复记录与任务不一致。",409)
            job=self.catalog.get_job(job_id)
            if current["state"]=="cancelled": return job
            if current["state"] not in ("pending","running") or not current["payload"].get("cancelCleanup"):
                raise ArchiveError("导入没有可认领的取消清理记录。",409)
            payload=current["payload"]
            try:
                self.catalog.update_job(job_id,state="running",phase="cancellation_cleanup",
                                        message="取消已记录，正在清理该任务的暂存原件。")
                self.vault.write_journal(current)
                if self.vault.object_path(payload["objectId"]).exists():
                    self.vault.remove_object(payload["objectId"])
                self.vault.discard_stage(payload["stageId"])
                self.vault.delete_journal(current["id"])
                # Only the completed filesystem barrier permits terminal cancellation.
                # PG intent remains recoverable if interrupted before this transaction.
                return self.catalog.finish_cancelled_import(job_id,current["id"],
                    expected_identity={"stageId":payload["stageId"],"objectId":payload["objectId"]})["job"]
            except Exception as exc:
                try:
                    return self.catalog.update_job(job_id,state="failed",phase="recovery_pending",
                        message="取消已记录；暂存清理尚未完成，重试或重启将恢复清理。",
                        error=self.error_payload(exc),result={"cleanupPending":True,"cancelled":False})
                except Exception:
                    return self.catalog.get_job(job_id)

    def _prepared_import(self,operation):
        manifest=operation["payload"].get("manifest")
        if not manifest: return None
        stage=self.vault.stage_path(operation["payload"]["stageId"])
        expected=manifest.get("currentSha256") or manifest["sha256"]
        if hash_file(self.vault.checked(stage/"source.zip",self.vault.staging))!=expected:
            raise ArchiveError("已准备的导入 ZIP 校验失败，恢复已暂停。",503)
        actual=json.loads(self.vault.checked(stage/"metadata.json",self.vault.staging).read_text(encoding="utf-8-sig"))
        if actual!=manifest: raise ArchiveError("导入恢复清单与持久意图不一致。",503)
        for member in manifest["files"]:
            path=self.vault.checked(stage/"files"/Path(*PurePosixPath(safe_name(member["path"])).parts),self.vault.staging)
            if path.stat().st_size!=member["size"] or hash_file(path)!=member["sha256"]:
                raise ArchiveError("已准备的导入成员校验失败，恢复已暂停。",503)
        documents={r["id"]:self.vault.checked(stage/"files"/Path(*PurePosixPath(safe_name(r["md"])).parts),self.vault.staging).read_text(encoding="utf-8-sig") for r in manifest["reports"]}
        return manifest,documents

    def _run_import(self,operation,recovery=False):
        payload=operation["payload"]; job_id=operation.get("jobId") or payload["jobId"]
        stage_id=payload["stageId"]; object_id=payload["objectId"]
        committed=False
        last_update=[0.0,None]; last_cancel=[0.0,False]
        def cancelled(force=False):
            if self._closing.is_set():
                raise ArchiveError("应用正在关闭；导入原件已保留，下次启动继续。",503,code="shutdown_paused")
            now=time.monotonic()
            if force or now-last_cancel[0]>0.3:
                job=self.catalog.get_job(job_id) or {}
                last_cancel[:]=[now,bool(job.get("cancelRequested") or job.get("state")=="cancelled")]
            return last_cancel[1]
        def progress(phase,value):
            now=time.monotonic()
            if phase!=last_update[1] or now-last_update[0]>=1.0:
                self.catalog.update_job(job_id,state="running",phase=phase,progress=value,message="正在校验和保留完整原件。")
                last_update[:]=[now,phase]
        try:
            with self.library_lock():
                with self._mutations:
                    claim=self.catalog.claim_import(job_id,operation["id"],recover=recovery,
                        expected_identity={"stageId":stage_id,"objectId":object_id})
                current=claim["operation"]
                if not claim["claimed"]:
                    if current and current["state"] in ("pending","running"):
                        if current["payload"].get("cancelCleanup"):
                            self._finish_import_cancellation(current)
                        elif claim["job"].get("cancelRequested"):
                            current=self.catalog.update_operation(current["id"],payload={"cancelCleanup":True},state="running")
                            self._finish_import_cancellation(current)
                    return
                operation=current
                if operation["payload"].get("cancelCleanup"):
                    self._finish_import_cancellation(operation); return
                if self.vault.object_path(object_id).exists():
                    manifest=self.vault.manifest_at(object_id); documents=documents_at(self.vault,manifest)
                else:
                    prepared=self._prepared_import(operation)
                    manifest,documents=prepared if prepared else prepare_zip(self.vault,stage_id,object_id,payload["sourceName"],cancelled,progress)
                if cancelled(): raise ArchiveError("导入已取消。",409,code="cancelled")
                with self._mutations:
                    # Cancellation accepted while waiting for another commit must
                    # be observed before this task crosses its commit boundary.
                    if cancelled(force=True): raise ArchiveError("导入已取消。",409,code="cancelled")
                    revised=self.catalog.resolve_import_identity(manifest)
                    if revised!=manifest:
                        if self.vault.object_path(object_id).exists():
                            raise ArchiveError("已提升原件的身份不能改写，恢复已暂停。",503)
                        documents={new["id"]:documents[old["id"]] for old,new in zip(manifest["reports"],revised["reports"])}
                        manifest=revised
                        atomic_json(self.vault.checked(self.vault.stage_path(stage_id)/"metadata.json",self.vault.staging),manifest)
                    operation=self.catalog.update_operation(operation["id"],payload={"manifest":manifest})
                    self.vault.write_journal(operation)
                    self.catalog.update_job(job_id,state="running",phase="committing",progress=0.95,message="正在提交资料目录。")
                    self.vault.promote(stage_id,object_id)
                    result=self.catalog.finalize_import(job_id,manifest,documents,object_members=manifest["files"])
                    committed=True
                self.vault.delete_journal(operation["id"])
                result["message"]="已保存 "+str(len(result.get("reports",[])))+" 期报告，完整原件与目录已提交。"
                self.catalog.update_job(job_id,state="succeeded",phase="complete",progress=1,message=result["message"],result=result)
        except Exception as exc:
            error=self.error_payload(exc)
            if error.get("code")=="shutdown_paused":
                try: self.catalog.update_job(job_id,state="failed",phase="paused",message=str(exc),
                    error=error,result={"paused":True,"cancelled":False})
                except Exception: pass
                return
            is_cancelled=error.get("code")=="cancelled" or "取消" in str(exc)
            terminal=not committed and getattr(exc,"status",500)<500
            try:
                with self.library_lock():
                    if terminal and is_cancelled:
                        current=self.catalog.update_operation(operation["id"],payload={"cancelCleanup":True},state="running")
                        self._finish_import_cancellation(current); return
                    if terminal:
                        if self.vault.object_path(object_id).exists(): self.vault.remove_object(object_id)
                        self.vault.discard_stage(stage_id)
                        self.catalog.update_operation(operation["id"],state="failed")
                        self.vault.delete_journal(operation["id"])
                    self.catalog.update_job(job_id,state="succeeded" if committed else "failed",
                                            phase="failed",message=str(exc),error=error)
            except Exception: pass

    @staticmethod
    def error_payload(exc):
        status=getattr(exc,"status",500)
        code=getattr(exc,"code",None) or ("conflict" if status==409 else "not_found" if status==404 else "invalid_request" if status<500 else "storage_unavailable")
        return {"error":str(exc) if status<500 or hasattr(exc,"status") else "操作暂未完成；已保留恢复记录，请重启或检查资料库。",
                "code":code,"status":status,"existing":getattr(exc,"existing",None)}

    def retained_files(self,manifest,survivors):
        declared=set().union(*({r["md"],r["html"],*r.get("attachments",[])} for r in manifest["reports"]))
        sources={r[k] for r in survivors for k in ("md","html")}
        removed={r[k] for r in manifest["reports"] for k in ("md","html")}-sources
        files={m["path"] for m in manifest["files"]}
        keep=(set().union(*({r["md"],r["html"],*r.get("attachments",[])} for r in survivors)) | (files-declared))-removed
        pending=list(keep); visited=set()
        while pending:
            name=pending.pop()
            if name in visited: continue
            visited.add(name)
            suffix=PurePosixPath(name).suffix.lower()
            if suffix not in (".md",".html",".htm",".css",".svg",".js",".json",".txt",".xml"): continue
            try: text=unquote(unescape(self.vault.member_path(manifest,name).read_text(encoding="utf-8-sig"))).casefold()
            except UnicodeError:
                keep.update(files-removed); continue
            if suffix==".js" or ("\\" in text and (suffix==".css" or "url" in text or "@import" in text)) or (suffix in (".html",".htm") and "script" in scan_html(text).unsupported):
                keep.update(files-removed); continue
            for candidate in files:
                if candidate not in removed and candidate not in keep and PurePosixPath(candidate).name.casefold() in text:
                    keep.add(candidate); pending.append(candidate)
        return sorted(keep)

    def purge(self,rid,changes):
        self.require_writable()
        self.validate_rid(rid)
        allowed={"confirm","trashedAt","trashToken","reportGeneration","generation"}
        if not isinstance(changes,dict) or not set(changes).issubset(allowed) or changes.get("confirm")!=rid or not isinstance(changes.get("trashedAt"),str) or not changes["trashedAt"]:
            raise ArchiveError("永久删除必须确认当前报告及回收站时间。")
        if not self._slots.acquire(blocking=False): raise ArchiveError("后台任务已满，请稍后重试。",429)
        new_job_id=uuid.uuid4().hex
        operation=None
        try:
            with self.library_lock(),self._mutations:
                pending=self.catalog.pending_purge(rid,changes["trashedAt"])
                projection_plan=pending["payload"].get("migrationProjectionPlan") if pending else self.vault.projection_plan()
                self.catalog.create_job("purge",{"reportId":rid},job_id=new_job_id)
                operation=self.catalog.begin_purge(rid,changes,payload={"jobId":new_job_id,"migrationProjectionPlan":projection_plan},generation=changes.get("generation"))
                payload=operation["payload"]
                job_id=operation.get("jobId") or payload.get("jobId")
                if job_id != new_job_id:
                    self.catalog.update_job(new_job_id,state="cancelled",message="已保留现有清理任务，无需重复提交。")
                if operation.get("state")=="complete":
                    self._slots.release()
                    return {"id":rid,"deleted":True,"cleanupPending":False,"jobId":job_id,
                            "message":"报告已永久删除，无法从回收站恢复。"}
                self.vault.write_journal(operation)
            self._submit(self._run_purge,operation,slot_reserved=True)
            return {"id":rid,"deleted":True,"cleanupPending":True,"jobId":job_id,
                    "message":"永久删除已提交，正在清理原件。"}
        except Exception as exc:
            if operation is None:
                try: self.catalog.update_job(new_job_id,state="failed",message=str(exc),error=self.error_payload(exc))
                except Exception: pass
            self._slots.release()
            raise

    def _run_purge(self,operation):
        payload=operation["payload"]; job_id=operation.get("jobId") or payload.get("jobId")
        package=operation.get("package") or payload.get("package")
        try:
            with self.library_lock(),self.package_lock(package):
                self.catalog.update_job(job_id,state="running",phase="cleaning",progress=0.05,message="正在清理原件。")
                old=payload["oldManifest"]; old_id=payload.get("oldObjectId") or old["objectId"]
                report_id=operation.get("reportId") or payload.get("reportId")
                if self.cache: self.cache.invalidate_report(report_id)
                survivors=[r for r in old["reports"] if r["id"]!=report_id]
                new_id=payload.get("newObjectId") or uuid.uuid4().hex
                stage_id=payload.get("stageId") or uuid.uuid4().hex
                keep=payload.get("keepPaths")
                if keep is None: keep=self.retained_files(old,survivors) if survivors else []
                planned={**old,"objectId":new_id,"reports":survivors}
                operation=self.catalog.update_operation(operation["id"],payload={"newObjectId":new_id,"stageId":stage_id,"keepPaths":keep})
                self.vault.write_journal(operation)
                new_manifest=None
                if survivors:
                    if self.vault.object_path(new_id).exists(): new_manifest=self.vault.manifest_at(new_id)
                    else:
                        new_manifest=self.vault.build_survivor(old,planned,keep,stage_id)
                        self.vault.promote(stage_id,new_id)
                self.catalog.update_job(job_id,phase="cleaning",progress=0.8)
                # Clear only audited, library-owned projections. Unknown/changed
                # evidence keeps the durable purge blocked rather than claiming erasure.
                self.vault.clear_projections(operation["payload"].get("migrationProjectionPlan"))
                # New survivor bytes are durable before old bytes are removed. Package stays blocked.
                self.vault.remove_object(old_id)
                result=self.catalog.complete_purge(operation["id"],new_manifest,object_members=new_manifest["files"] if new_manifest else None)
                self.vault.discard_stage(stage_id)
                self.vault.delete_journal(operation["id"])
                result={**(result or {}),"id":report_id,"deleted":True,"cleanupPending":False,"message":"报告已永久删除，无法从回收站恢复。"}
                self.catalog.update_job(job_id,state="succeeded",phase="complete",progress=1,message=result["message"],result=result)
        except Exception as exc:
            try: self.catalog.update_job(job_id,state="failed",phase="recovery_pending",message="删除已提交，清理仍待恢复；资料包保持封锁。",error=self.error_payload(exc))
            except Exception: pass

    def recover(self):
        # Capture a stable (PG creation time, id) boundary before HTTP starts. New
        # HTTP operations belong to their accepting worker, never to this recovery pass.
        tail=self.catalog.pending_operations(limit=1,reverse=True)
        if not tail: return
        upper=(tail[0]["createdAt"],tail[0]["id"])
        self._recovery_thread=threading.Thread(target=self._recover_pending,args=(upper,),
            name="library-recovery",daemon=True)
        self._recovery_thread.start()

    def _recover_pending(self,upper):
        cursor=None
        try:
            while not self._closing.is_set():
                page=self.catalog.pending_operations(limit=8,after=cursor,through=upper)
                if not page: return
                for operation in page:
                    if self._closing.is_set(): return
                    cursor=(operation["createdAt"],operation["id"])
                    kind=operation.get("kind")
                    if kind not in ("import","purge"): continue
                    while not self._slots.acquire(timeout=0.1):
                        if self._closing.is_set(): return
                    if self._closing.is_set(): self._slots.release(); return
                    if kind=="import":
                        self._submit(self._run_import,operation,True,slot_reserved=True,job_id=operation.get("jobId"))
                    else:
                        self._submit(self._run_purge,operation,slot_reserved=True)
        except Exception as exc:
            self._recovery_error=self.error_payload(exc)

    def job(self,job_id):
        if not JOBID.fullmatch(job_id): raise ArchiveError("任务标识无效。",404)
        return {"job":public_job(self.catalog.get_job(job_id))}

    def cancel_job(self,job_id):
        self.require_writable()
        job=self.job(job_id)["job"]
        if job.get("kind")=="purge": raise ArchiveError("永久删除已经提交，清理任务会继续完成。",409)
        if job.get("phase")=="committing": raise ArchiveError("资料目录正在提交，请等待任务完成。",409)
        operation=self.catalog.get_import_operation(job_id)
        if operation and operation["state"] in ("pending","running"):
            claimed=self.catalog.cancel_queued_import(job_id,operation["id"],
                expected_identity={key:operation["payload"][key] for key in ("stageId","objectId")})
            current=claimed["operation"]
            if claimed["claimed"] or current["payload"].get("cancelCleanup"):
                self._cancel_import_future(job_id)
                return {"job":public_job(self._finish_import_cancellation(current))}
        return {"job":public_job(self.catalog.request_cancel(job_id))}

    def health(self):
        info=self.catalog.health()
        return {"app":"reference-library","version":2,"patch":PATCH_VERSION,"storageBackend":"postgresql",
                "instanceId":self.instance_id,
                "libraryId":self.library_id,
                "readOnly":self.read_only,
                "capabilities":{key:not self.read_only for key in ("import","metadata","trash","purge","preferences")},
                "renderCache":self.cache.diagnostics() if self.cache else {"enabled":False},
                "schemaVersion":info.get("schemaVersion",info.get("schema_version")),
                **{k:info[k] for k in ("generation","pendingOperations","pendingDeletions","ready") if k in info}}

    def close(self):
        with self._close_guard:
            if self._resources_closed: return
            self._closing.set()
            errors=[]
            if self._recovery_thread and self._recovery_thread.is_alive() and self._recovery_thread is not threading.current_thread():
                try: self._recovery_thread.join()
                except Exception as exc: errors.append(exc)
            if self._executor:
                try: self._executor.shutdown(wait=True,cancel_futures=True)
                except Exception as exc: errors.append(exc)
            for resource in (self._lease,self.cache,self.catalog):
                if resource:
                    try: resource.close()
                    except Exception as exc: errors.append(exc)
            self._resources_closed=True
            if errors: raise errors[0]

def make_handler(store,port):
    class Handler(BaseHTTPRequestHandler):
        server_version="ReferenceLibrary/2"
        def log_message(self,*args): pass
        def host_ok(self):
            valid=(f"http://127.0.0.1:{port}",f"http://localhost:{port}")
            return self.headers.get("Host","") in (f"127.0.0.1:{port}",f"localhost:{port}") and self.headers.get("Origin") in (None,*valid) and self.headers.get("Sec-Fetch-Site")!="cross-site"
        def reply(self,status,body,ctype="application/json; charset=utf-8",extra=None):
            if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(body)))
                self._security(extra); self.end_headers(); self.wfile.write(body)
            except (ConnectionError,TimeoutError): pass
        def _security(self,extra=None):
            self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Cache-Control","no-store")
            self.send_header("Referrer-Policy","no-referrer"); self.send_header("X-Frame-Options","SAMEORIGIN")
            if extra:
                for key,value in extra.items(): self.send_header(key,value)
        def send_file(self,target,ctype,extra=None):
            with target.open("rb") as inp:
                size=os.fstat(inp.fileno()).st_size
                self.send_response(200); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(size))
                self._security(extra); self.end_headers()
                while chunk:=inp.read(CHUNK): self.wfile.write(chunk)
        def error(self,exc):
            payload=store.error_payload(exc)
            return self.reply(payload.pop("status"),payload)
        def do_GET(self):
            if not self.host_ok(): return self.reply(403,{"error":"请求来源无效。","code":"forbidden"})
            route=urlsplit(self.path)
            try:
                path=unquote(route.path,errors="strict")
                if "\x00" in path or "\\" in path: raise ArchiveError("路径无效。",404)
                params=parse_qs(route.query,max_num_fields=20)
                if path=="/api/health": return self.reply(200,store.health())
                if path=="/api/library":
                    offset=int(params.get("offset",["0"])[0]); limit=int(params.get("limit",params.get("pageSize",["50"]))[0])
                    generation=int(params["generation"][0]) if params.get("generation") else None
                    if offset<0 or offset>5000 or not 1<=limit<=200: raise ArchiveError("分页范围无效。")
                    return self.reply(200,store.list(params.get("q",[""])[0],params.get("category",[""])[0],
                        params.get("trash",["0"])[0]=="1",offset,limit,params.get("cursor",[None])[0],generation))
                if path.startswith("/api/jobs/"): return self.reply(200,store.job(path.rsplit("/",1)[1]))
                if path.startswith("/api/report/"): return self.reply(200,store.reader(path.rsplit("/",1)[1]))
                if path.startswith("/api/original/") or path.startswith("/api/raw/"):
                    with store.readable(path.rsplit("/",1)[1]) as (record,manifest):
                        if path.startswith("/api/original/"):
                            target=store.vault.source_path(manifest); filename=record["sourceName"]; ctype="application/zip"
                        else:
                            kind=params.get("type",["md"])[0]
                            if kind not in ("md","html"): raise ArchiveError("原件类型无效。")
                            target=store.vault.member_path(manifest,record[kind]); filename=PurePosixPath(record[kind]).name; ctype="application/octet-stream"
                        return self.send_file(target,ctype,{"Content-Disposition":"attachment; filename*=UTF-8''"+quote(filename)})
                if path.startswith("/reports/"):
                    parts=path.split("/")
                    if len(parts)<5 or parts[3]!="files" or any(p in ("",".","..") for p in parts[4:]): raise ArchiveError("资源路径无效。",404)
                    with store.library_lock(),store.package_lock(parts[2]):
                        manifest=store.package(parts[2],reference_only=True)
                        member=store.catalog.get_member(parts[2],"/".join(parts[4:]))
                        if member is None: raise ArchiveError("Resource not found.",404)
                        manifest["files"]=[member]
                        target=store.vault.member_path(manifest,"/".join(parts[4:]))
                        ctype=MIME.get(target.suffix.lower(),mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                        return self.send_file(target,ctype,{"Content-Security-Policy":CONTENT_CSP})
                target=(ROOT/"web"/("index.html" if path=="/" else path.lstrip("/"))).absolute()
                web=(ROOT/"web").resolve()
                if not target.is_relative_to(web): raise ArchiveError("页面不存在。",404)
                OriginalStore._plain_ancestors(target)
                if not target.resolve().is_relative_to(web) or not target.is_file(): raise ArchiveError("页面不存在。",404)
                return self.send_file(target,MIME.get(target.suffix.lower(),"application/octet-stream"),{"Content-Security-Policy":APP_CSP})
            except (ConnectionError,TimeoutError): pass
            except (ValueError,UnicodeError) as exc: self.error(ArchiveError("请求参数无效。"))
            except Exception as exc: self.error(exc)
        def do_POST(self):
            if not self.host_ok() or self.headers.get("Origin") not in (f"http://127.0.0.1:{port}",f"http://localhost:{port}") or self.headers.get("X-Moon-Request")!="1":
                return self.reply(403,{"error":"请求来源无效。","code":"forbidden"})
            try:
                self.connection.settimeout(30)
                if self.headers.get("Transfer-Encoding"): raise ArchiveError("请使用带长度的请求。",400)
                length=int(self.headers.get("Content-Length","0")); path=urlsplit(self.path).path
                if path=="/api/import":
                    if not 1<=length<=MAX_ZIP: raise ArchiveError("ZIP 为空或超过 100 MB。",413)
                    filename=unquote(self.headers.get("X-Filename","report.zip"),errors="strict")
                    if len(filename)>240 or any(ord(c)<32 for c in filename): raise ArchiveError("上传文件名无效。")
                    class UploadReader:
                        remaining=length
                        def read(reader,count):
                            chunk=self.rfile.read(min(count,reader.remaining))
                            reader.remaining-=len(chunk)
                            return chunk
                    upload=UploadReader()
                    try: result=store.accept_upload(upload,length,filename)
                    except Exception:
                        # Drain only the authenticated, bounded upload body. Closing a
                        # Windows socket with unread body bytes can reset it before
                        # the caller receives the JSON admission error.
                        try:
                            while upload.remaining:
                                if not upload.read(CHUNK): break
                        except (ConnectionError,TimeoutError,OSError): pass
                        raise
                    return self.reply(202,result)
                if not 1<=length<=65536: raise ArchiveError("JSON 请求为空或超过 64 KB。",413)
                raw=self.rfile.read(length)
                if len(raw)!=length: raise ArchiveError("请求未完成。")
                changes=json.loads(raw)
                if path.startswith("/api/metadata/"): return self.reply(200,store.update_metadata(path.rsplit("/",1)[1],changes))
                if path.startswith("/api/trash/"): return self.reply(200,store.set_trash(path.rsplit("/",1)[1],changes))
                if path.startswith("/api/purge/"):
                    result=store.purge(path.rsplit("/",1)[1],changes)
                    return self.reply(202 if result["cleanupPending"] else 200,result)
                if re.fullmatch(r"/api/jobs/[a-f0-9-]{20,80}/cancel",path):
                    return self.reply(200,store.cancel_job(path.split("/")[3]))
                if path=="/api/settings": return self.reply(200,store.update_preferences(changes))
                if path=="/api/shutdown":
                    self.reply(200,{"message":"正在保存状态并关闭应用。"})
                    threading.Thread(target=self.server.shutdown,daemon=True).start(); return
                raise ArchiveError("接口不存在。",404)
            except (ConnectionError,TimeoutError): pass
            except (ValueError,UnicodeError): self.error(ArchiveError("JSON 或请求参数无效。"))
            except Exception as exc: self.error(exc)
    return Handler

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=18761)
    platform_root=Path(os.environ.get("LOCALAPPDATA",Path.home()/"AppData"/"Local")) if os.name=="nt" else Path.home()/"Library"/"Application Support" if sys.platform=="darwin" else Path(os.environ.get("XDG_DATA_HOME",Path.home()/".local"/"share"))
    default_data=os.environ.get("REFERENCE_LIBRARY_DATA") or str(platform_root/"ReferenceLibrary"/"Library")
    parser.add_argument("--data",default=default_data)
    parser.add_argument("--cache",default=os.environ.get("REFERENCE_LIBRARY_CACHE"))
    parser.add_argument("--read-only",action="store_true",default=os.environ.get("REFERENCE_LIBRARY_READ_ONLY")=="1")
    args=parser.parse_args()
    if not 1024<=args.port<=65535: parser.error("port must be between 1024 and 65535")
    store=Store(args.data,cache_root=args.cache,read_only=args.read_only)
    server=ThreadingHTTPServer(("127.0.0.1",args.port),make_handler(store,args.port)); server.daemon_threads=True
    print(f"Reference Library: http://127.0.0.1:{args.port}",flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); store.close()

if __name__=="__main__": main()

