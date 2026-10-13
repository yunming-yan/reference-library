"""Development tests: immutable bytes, ZIP budgets, purge ownership and HTTP errors."""
import ast, io, json, os, sys, tempfile, unittest, uuid, zipfile
from pathlib import Path
APP=Path(__file__).resolve().parents[1]/"app"
sys.path.insert(0,str(APP))
from original_store import OriginalStore, OriginalError, atomic_json
from ingest import prepare_zip, render_md
from server import Store, make_handler

def zip_bytes(reports=2):
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        for number in range(reports):
            stem="2026-10-13_第"+str(number+1)+"期_测试"
            md="# 全文 "+str(number)+"\n\n## 第二节\n\n表格\n\n| 名称 | 数值 |\n| --- | --- |\n| 测试 | 42 |\n\n![图](../附件/图"+str(number)+".svg)\n\nUnicode Straße \x00 保留\n"
            archive.writestr("md/原文/"+stem+".md",md.encode("utf-8"))
            archive.writestr("htm/"+stem+".html",("<article><h1>全文 "+str(number)+"</h1><p>全部正文</p></article>").encode("utf-8"))
            archive.writestr("md/附件/图"+str(number)+".svg",b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="#CC4422" d="M0 0h10v10z"/></svg>')
        archive.writestr("notes.txt",b"unknown shared ancillary bytes")
    return out.getvalue()

class OriginalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="backend-",dir=os.environ.get("BACKEND_TEST_ROOT",tempfile.gettempdir()))
        self.vault=OriginalStore(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def prepare(self,raw):
        stage_id=uuid.uuid4().hex; object_id=uuid.uuid4().hex
        stage=self.vault.new_stage(stage_id)
        (stage/"source.zip").write_bytes(raw)
        manifest,docs=prepare_zip(self.vault,stage_id,object_id,"test.zip")
        self.vault.promote(stage_id,object_id)
        return manifest,docs

    def test_exact_originals_unicode_nul_and_tables(self):
        raw=zip_bytes(); manifest,docs=self.prepare(raw)
        self.assertEqual(self.vault.source_path(manifest).read_bytes(),raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for member in manifest["files"]:
                self.assertEqual(self.vault.member_path(manifest,member["path"]).read_bytes(),archive.read(member["path"]))
        self.assertEqual(len(manifest["reports"]),2)
        self.assertTrue(all("\x00" in text for text in docs.values()))
        self.assertEqual(manifest["reports"][0]["tableCount"],1)
        self.assertEqual(manifest["reports"][0]["issue"],"01")
        self.assertTrue((Path(self.temp.name)/".reference-library-owner.json").exists())

    def test_primary_original_dir_contract_with_nested_attachment(self):
        output=io.BytesIO()
        md_name="md/原件/2026-10-13_第01期_合同.md"
        html_name="htm/2026-10-13_第01期_合同.html"
        asset_name="md/附件/2026-10-13_第01期/结构图.svg"
        md="# 原件全文\n\n![结构图](../附件/2026-10-13_第01期/结构图.svg)\n\n全部正文完整保留。".encode("utf-8")
        html="<article><h1>原件全文</h1><p>全部正文完整保留。</p></article>".encode("utf-8")
        svg=b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="#CC4422" d="M0 0h10v10z"/></svg>'
        with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(md_name,md); archive.writestr(html_name,html); archive.writestr(asset_name,svg)
        raw=output.getvalue(); manifest,documents=self.prepare(raw)
        report=manifest["reports"][0]
        self.assertEqual(report["md"],md_name); self.assertEqual(report["html"],html_name)
        self.assertEqual(report["attachments"],[asset_name])
        self.assertEqual(documents[report["id"]],md.decode("utf-8"))
        self.assertEqual(self.vault.source_path(manifest).read_bytes(),raw)
        self.assertEqual(self.vault.member_path(manifest,asset_name).read_bytes(),svg)

    def test_survivor_zip_excludes_deleted_sources_and_assets(self):
        old,_=self.prepare(zip_bytes())
        store=Store.__new__(Store); store.vault=self.vault
        survivor=old["reports"][1]; keep=store.retained_files(old,[survivor])
        self.assertNotIn(old["reports"][0]["md"],keep)
        self.assertNotIn(old["reports"][0]["attachments"][0],keep)
        self.assertIn("notes.txt",keep)
        stage_id=uuid.uuid4().hex; new_id=uuid.uuid4().hex
        new=self.vault.build_survivor(old,{**old,"objectId":new_id,"reports":[survivor]},keep,stage_id)
        self.vault.promote(stage_id,new_id); self.vault.remove_object(old["objectId"])
        self.assertFalse(self.vault.object_path(old["objectId"]).exists())
        with zipfile.ZipFile(self.vault.source_path(new)) as archive:
            self.assertEqual(set(archive.namelist()),set(keep))
            self.assertEqual(archive.read(survivor["md"]),self.vault.member_path(new,survivor["md"]).read_bytes())

    def test_zip_paths_and_case_collisions_rejected(self):
        for names in (["../escape.md"],["C:/evil.md"],["a\\b.md"],["x.","x"],["A.md","a.md"]):
            out=io.BytesIO()
            with zipfile.ZipFile(out,"w") as archive:
                for name in names: archive.writestr(name,b"bad")
            stage_id=uuid.uuid4().hex; stage=self.vault.new_stage(stage_id)
            (stage/"source.zip").write_bytes(out.getvalue())
            with self.assertRaises(OriginalError): prepare_zip(self.vault,stage_id,uuid.uuid4().hex,"evil.zip")

    def test_symlink_member_rejected(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,"w") as archive:
            member=zipfile.ZipInfo("md/原文/link.md"); member.create_system=3; member.external_attr=(0o120777<<16)
            archive.writestr(member,"../../outside")
        stage_id=uuid.uuid4().hex; stage=self.vault.new_stage(stage_id)
        (stage/"source.zip").write_bytes(out.getvalue())
        with self.assertRaises(OriginalError): prepare_zip(self.vault,stage_id,uuid.uuid4().hex,"link.zip")

    def test_serial_parallel_members_and_documents_match(self):
        raw=zip_bytes()
        outputs=[]
        for workers in (1,4):
            stage_id=uuid.uuid4().hex; object_id=uuid.uuid4().hex
            stage=self.vault.new_stage(stage_id);(stage/"source.zip").write_bytes(raw)
            manifest,documents=prepare_zip(self.vault,stage_id,object_id,"test.zip",extract_workers=workers)
            outputs.append((manifest,documents))
        self.assertEqual(outputs[0][0]["contentSha256"],outputs[1][0]["contentSha256"])
        self.assertEqual(outputs[0][0]["files"],outputs[1][0]["files"])
        self.assertEqual(outputs[0][1],outputs[1][1])

    def test_parallel_crc_failure_never_produces_ready_manifest(self):
        raw=bytearray(zip_bytes())
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info=next(member for member in archive.infolist() if member.filename.endswith(".md"))
            offset=info.header_offset
            name_len=int.from_bytes(raw[offset+26:offset+28],"little")
            extra_len=int.from_bytes(raw[offset+28:offset+30],"little")
            start=offset+30+name_len+extra_len
            raw[start+max(0,info.compress_size//2)]^=0x08
        stage_id=uuid.uuid4().hex; object_id=uuid.uuid4().hex
        stage=self.vault.new_stage(stage_id);(stage/"source.zip").write_bytes(raw)
        with self.assertRaises(OriginalError):prepare_zip(self.vault,stage_id,object_id,"bad-crc.zip",extract_workers=4)
        self.assertFalse((stage/"metadata.json").exists())
        self.assertFalse(self.vault.object_path(object_id).exists())

    def test_every_parallel_fsync_finishes_before_ready(self):
        import threading,time
        stage_id=uuid.uuid4().hex; object_id=uuid.uuid4().hex
        stage=self.vault.new_stage(stage_id);(stage/"source.zip").write_bytes(zip_bytes())
        entered=threading.Event();released=threading.Event();completed=threading.Event()
        sync_lock=threading.Lock();blocked=[False];errors=[];real_sync=os.fsync
        def controlled_sync(fd):
            if threading.current_thread().name.startswith("zip-member"):
                with sync_lock:
                    should_block=not blocked[0];blocked[0]=True
                if should_block:entered.set();released.wait(10)
            return real_sync(fd)
        def run():
            try:prepare_zip(self.vault,stage_id,object_id,"barrier.zip",extract_workers=4)
            except Exception as exc:errors.append(exc)
            finally:completed.set()
        os.fsync=controlled_sync;thread=threading.Thread(target=run)
        try:
            thread.start();self.assertTrue(entered.wait(5))
            time.sleep(.05);self.assertFalse(completed.is_set())
            self.assertFalse((stage/"metadata.json").exists())
            released.set();thread.join(10)
            self.assertTrue(completed.is_set());self.assertFalse(errors)
            self.assertTrue((stage/"metadata.json").exists())
        finally:released.set();thread.join(10);os.fsync=real_sync

    def test_membership_and_ownership_boundary(self):
        manifest,_=self.prepare(zip_bytes())
        with self.assertRaises(OriginalError): self.vault.member_path(manifest,"../../other.txt")
        with self.assertRaises(OriginalError): self.vault.member_path(manifest,"missing.txt")
        marker=Path(self.temp.name)/".reference-library-owner.json"
        atomic_json(marker,{"format":1,"owner":"someone-else","libraryId":str(uuid.uuid4())})
        with self.assertRaises(OriginalError): OriginalStore(self.temp.name)

    def test_bounded_extraction_checks_declared_expanded_budget(self):
        import ingest
        out=io.BytesIO()
        with zipfile.ZipFile(out,"w",compression=zipfile.ZIP_DEFLATED) as archive: archive.writestr("large.bin",b"x"*3000)
        stage_id=uuid.uuid4().hex; stage=self.vault.new_stage(stage_id); (stage/"source.zip").write_bytes(out.getvalue())
        before=ingest.MAX_EXPANDED
        ingest.MAX_EXPANDED=2000
        try:
            with self.assertRaises(OriginalError) as caught: prepare_zip(self.vault,stage_id,uuid.uuid4().hex,"bomb.zip")
            self.assertEqual(caught.exception.status,413)
        finally: ingest.MAX_EXPANDED=before

    def test_cancel_before_extraction_preserves_upload(self):
        stage_id=uuid.uuid4().hex; stage=self.vault.new_stage(stage_id); raw=zip_bytes(); (stage/"source.zip").write_bytes(raw)
        with self.assertRaises(OriginalError): prepare_zip(self.vault,stage_id,uuid.uuid4().hex,"cancel.zip",lambda:True)
        self.assertEqual((stage/"source.zip").read_bytes(),raw)

class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        class Stub:
            error_payload=staticmethod(Store.error_payload)
            def health(self): return {"app":"reference-library","version":2}
        self.http=ThreadingHTTPServer(("127.0.0.1",0),make_handler(Stub(),0))
        self.port=self.http.server_address[1]
        self.http.RequestHandlerClass=make_handler(Stub(),self.port)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()
    def request(self,method,path,body=None,headers=None):
        from http.client import HTTPConnection
        connection=HTTPConnection("127.0.0.1",self.port,timeout=5)
        connection.request(method,path,body=body,headers=headers or {})
        reply=connection.getresponse(); raw=reply.read(); status=reply.status
        response_headers=dict(reply.getheaders()); connection.close()
        return status,json.loads(raw),response_headers
    def test_host_origin_and_csrf(self):
        self.assertEqual(self.request("GET","/api/health")[0],200)
        self.assertEqual(self.request("GET","/api/health",headers={"Host":"evil.example"})[0],403)
        self.assertEqual(self.request("GET","/api/health",headers={"Origin":"http://evil.example"})[0],403)
        self.assertEqual(self.request("GET","/api/health",headers={"Sec-Fetch-Site":"cross-site"})[0],403)
        self.assertEqual(self.request("POST","/api/settings",b"{}",{"Origin":"http://127.0.0.1:"+str(self.port)})[0],403)
    def test_json_and_path_errors_are_structured(self):
        headers={"Origin":"http://127.0.0.1:"+str(self.port),"X-Moon-Request":"1"}
        status,error,response_headers=self.request("POST","/api/settings",b"{",headers)
        self.assertEqual(status,400); self.assertEqual(error["code"],"invalid_request")
        self.assertEqual(response_headers["X-Content-Type-Options"],"nosniff")
        self.assertEqual(self.request("GET","/../server.py")[0],404)
        self.assertEqual(self.request("GET","/%00bad")[0],404)
        self.assertEqual(self.request("POST","/api/settings",b"{}",{**headers,"Content-Length":"65537"})[0],413)

if __name__=="__main__": unittest.main(verbosity=2)

