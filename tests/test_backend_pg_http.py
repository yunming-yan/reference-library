"""Actual PostgreSQL + synthetic HTTP backend contract development test."""
import hashlib, io, json, os, sys, tempfile, threading, time, zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
APP=Path(__file__).resolve().parents[1]/"app"
sys.path.insert(0,str(APP))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from pg_catalog import Catalog
from server import Store, make_handler
from test_backend_api import zip_bytes
import server as backend

if __name__ != "__main__":
    import unittest
    raise unittest.SkipTest("Manual PostgreSQL harness requires explicit private test environment.")

config=json.loads(Path(os.environ["BACKEND_PG_CONFIG"]).read_text(encoding="utf-8-sig"))
connection={key:config[key] for key in ("host","port","dbname","user","password")}
root=Path(os.environ["BACKEND_PG_DATA"])
root.mkdir(parents=True,exist_ok=True)
outcome={"python":sys.version,"scope":"synthetic isolated PostgreSQL/HTTP, fault injection plus graceful restart","checks":[]}
store=None; http=None
def mark(name): outcome["checks"].append({"name":name,"result":"PASS"})
def start():
    global store,http,thread,port
    store=Store(root,Catalog(connection))
    http=ThreadingHTTPServer(("127.0.0.1",0),make_handler(store,0)); port=http.server_address[1]
    http.RequestHandlerClass=make_handler(store,port)
    thread=threading.Thread(target=http.serve_forever,daemon=True); thread.start()
def stop():
    global store,http
    if http: http.shutdown(); http.server_close(); thread.join(); http=None
    if store: store.close(); store=None
def request(method,path,body=None,json_body=None,raw=False,headers=None):
    data=json.dumps(json_body,ensure_ascii=False).encode("utf-8") if json_body is not None else body
    supplied={"Origin":"http://127.0.0.1:"+str(port),"X-Moon-Request":"1"} if method=="POST" else {}
    supplied.update(headers or {})
    conn=HTTPConnection("127.0.0.1",port,timeout=20); conn.request(method,path,body=data,headers=supplied)
    reply=conn.getresponse(); payload=reply.read(); status=reply.status; response_headers=dict(reply.getheaders()); conn.close()
    return status,payload if raw else json.loads(payload),response_headers
def job_wait(job_id,expected="succeeded",allow_retry=False):
    limit=time.monotonic()+30
    while time.monotonic()<limit:
        status,value,_=request("GET","/api/jobs/"+job_id)
        assert status==200,value
        job=value["job"]
        if job["state"] in ("succeeded","failed","cancelled"):
            if allow_retry and job["state"]=="failed" and any(op.get("jobId")==job_id for op in store.catalog.pending_operations()):
                time.sleep(.05); continue
            assert job["state"]==expected,job
            return job
        time.sleep(.05)
    raise AssertionError("Job timeout")
def upload(raw,expected="succeeded"):
    status,value,_=request("POST","/api/import",body=raw,headers={"X-Filename":"synthetic.zip","Content-Type":"application/zip"})
    assert status==202,value
    return job_wait(value["jobId"],expected)
def trash(rid,restore=False):
    status,value,_=request("POST","/api/trash/"+rid,json_body={"restore":restore})
    assert status==200,value
    return value["report"]
def purge(record,expected="succeeded"):
    status,value,_=request("POST","/api/purge/"+record["id"],json_body={"confirm":record["id"],"trashedAt":record["trashedAt"],"trashToken":record["trashToken"],"reportGeneration":record["reportGeneration"]})
    assert status in (200,202),value
    return job_wait(value["jobId"],expected) if value["cleanupPending"] else value

try:
    start()
    outcome["database"]=store.catalog.health()["serverVersion"]
    outcome["schemaVersion"]=store.catalog.health()["schemaVersion"]
    import psycopg,apsw
    outcome.update(driver=psycopg.pq.__impl__,libpq=psycopg.pq.version(),sqlite=apsw.sqlitelibversion(),apsw=apsw.apswversion(),renderer=backend.RENDERER_VERSION)
    health=store.health()
    assert health["libraryId"]==store.vault.library_id and health["instanceId"]
    mark("persistent library identity matches PostgreSQL and instance UUID is nonnull")
    # Optional retry cleanup requires the exact prior import task and its immutable
    # report id/importedAt/package identities; never infer ownership from a filename.
    if os.environ.get("BACKEND_PG_PREVIOUS_JOB"):
        previous=store.catalog.get_job(os.environ["BACKEND_PG_PREVIOUS_JOB"])
        expected=json.loads(os.environ["BACKEND_PG_CLEANUP_IDENTITIES"])
        def identity(record): return {key:record[key] for key in ("id","importedAt","package")}
        assert previous and previous["state"]=="succeeded"
        assert sorted([identity(r) for r in previous["result"]["reports"]],key=lambda r:r["id"])==sorted(expected,key=lambda r:r["id"])
        current=store.catalog.list_reports(limit=200)["reports"]+store.catalog.list_reports(trash=True,limit=200)["reports"]
        assert sorted([identity(r) for r in current],key=lambda r:r["id"])==sorted(expected,key=lambda r:r["id"]), "Unexpected QA data; no cleanup authorized"
        for owned in expected:
            record=store.find(owned["id"])
            assert identity(record)==owned
            purge(record if record.get("trashedAt") else trash(record["id"]))
    status,empty,_=request("GET","/api/library"); assert status==200 and empty["total"]==0; mark("empty PG catalogue")
    raw=zip_bytes()
    job=upload(raw); reports=job["result"]["reports"]; assert len(reports)==2; mark("async durable import succeeds")
    duplicate=upload(raw,"failed"); assert duplicate["error"]["status"]==409 and duplicate["error"]["existing"]; mark("duplicate import conflicts without overwrite")
    first=reports[0]; second=reports[1]
    status,reader,_=request("GET","/api/report/"+first["id"])
    assert status==200 and "\x00" in reader["markdownSource"] and "<table>" in reader["content"] and reader["generation"] is not None; mark("complete MD HTML source and generation")
    status,zipped,_=request("GET","/api/original/"+first["id"],raw=True); assert status==200 and zipped==raw
    status,md,_=request("GET","/api/raw/"+first["id"]+"?type=md",raw=True)
    assert status==200 and md.decode("utf-8")==reader["markdownSource"]; mark("ZIP and Markdown exact-byte download")
    asset=first["attachments"][0]
    status,svg,headers=request("GET","/reports/"+first["package"]+"/files/"+quote(asset),raw=True)
    assert status==200 and b"#CC4422" in svg and "script-src 'none'" in headers["Content-Security-Policy"]; mark("mapped SVG bytes and sandbox CSP")
    for query in ("全文","Straße","STRASSE","\x00","42"):
        status,matches,_=request("GET","/api/library?q="+quote(query)); assert status==200 and matches["filteredCount"]==2,(query,matches)
    mark("Unicode casefold short-query NUL exact search")
    status,page,_=request("GET","/api/library?limit=1"); assert status==200 and len(page["reports"])==1 and page["hasMore"]
    status,next_page,_=request("GET","/api/library?limit=1&cursor="+quote(page["nextCursor"])+"&generation="+str(page["generation"]))
    assert status==200 and next_page["reports"][0]["id"]!=page["reports"][0]["id"] and not next_page["hasMore"]; mark("generation-bound stable cursor pagination")
    status,settings,_=request("POST","/api/settings",json_body={"theme":"dark","reading":{"lineHeight":2,"fontSize":20}})
    assert status==200 and settings["reading"]["lineHeight"]==2; mark("PG reading preferences")
    old_generation=page["generation"]
    status,metadata,_=request("POST","/api/metadata/"+first["id"],json_body={"title":"Custom title","category":"Testing"})
    assert status==200 and metadata["report"]["title"]=="Custom title"
    status,stale,_=request("GET","/api/library?generation="+str(old_generation)); assert status==409
    status,current_md,_=request("GET","/api/raw/"+first["id"]+"?type=md",raw=True); assert current_md==md; mark("metadata changes preserve source and invalidate stale generation")
    old_trash=trash(first["id"]); repeated=trash(first["id"])
    assert old_trash["trashedAt"]==repeated["trashedAt"] and old_trash["trashToken"]==repeated["trashToken"]
    trash(first["id"],True)
    status,stale,_=request("POST","/api/purge/"+first["id"],json_body={"confirm":first["id"],"trashedAt":old_trash["trashedAt"]})
    assert status==409; mark("trash idempotence and restored stale confirmation rejection")
    current=trash(first["id"])
    status,stale,_=request("POST","/api/purge/"+first["id"],json_body={"confirm":first["id"],"trashedAt":old_trash["trashedAt"],"trashToken":old_trash["trashToken"]})
    assert status==409; mark("fresh trash token rejects old incarnation")
    old_object=store.catalog.get_package(first["package"])["objectId"]
    purge(current)
    assert not store.vault.object_path(old_object).exists()
    assert request("GET","/api/report/"+first["id"])[0]==404
    status,survivor_zip,_=request("GET","/api/original/"+second["id"],raw=True); assert status==200
    with zipfile.ZipFile(io.BytesIO(survivor_zip)) as archive:
        assert first["md"] not in archive.namelist() and first["html"] not in archive.namelist() and first["attachments"][0] not in archive.namelist()
        assert second["md"] in archive.namelist()
    assert request("GET","/reports/"+first["package"]+"/files/"+quote(first["attachments"][0]))[0]==404
    mark("partial purge erases old object and exposes only surviving ZIP/resources")
    remaining=trash(second["id"]); purge(remaining)
    assert request("GET","/api/library")[1]["total"]==0 and not any(store.vault.originals.iterdir())
    assert request("POST","/api/purge/"+remaining["id"],json_body={"confirm":remaining["id"],"trashedAt":remaining["trashedAt"]})[0]==200
    mark("whole-package purge and completed retry")
    reimport=upload(raw); assert len(reimport["result"]["reports"])==2
    status,stale,_=request("POST","/api/purge/"+first["id"],json_body={"confirm":first["id"],"trashedAt":current["trashedAt"]})
    assert status==409; mark("reimported same report id rejects prior delete authorization")
    # Deterministic development cancellation before parsing; no committed report disappears.
    original_prepare=backend.prepare_zip; entered=threading.Event(); released=threading.Event()
    def paused(*args,**kwargs):
        entered.set(); released.wait(10); return original_prepare(*args,**kwargs)
    backend.prepare_zip=paused
    try:
        cancel_raw=zip_bytes(1)+b"valid ZIP trailing bytes"
        status,accepted,_=request("POST","/api/import",body=cancel_raw); assert status==202
        assert entered.wait(5)
        assert request("POST","/api/jobs/"+accepted["jobId"]+"/cancel",json_body={})[0]==200
        released.set(); job_wait(accepted["jobId"],"cancelled")
        assert request("GET","/api/library")[1]["total"]==2
    finally: released.set(); backend.prepare_zip=original_prepare
    mark("durable job cancellation before commit")
    # Fault injection verifies committed deletion remains hidden and its package blocked until restart.
    first_new=reimport["result"]["reports"][0]; second_new=reimport["result"]["reports"][1]
    pending=trash(first_new["id"]); original_remove=store.vault.remove_object
    def fail_remove(object_id): raise OSError("synthetic locked-file failure")
    store.vault.remove_object=fail_remove
    failed=purge(pending,"failed")
    assert request("GET","/api/report/"+second_new["id"])[0]==503
    assert request("GET","/api/report/"+first_new["id"])[0]==404
    store.vault.remove_object=original_remove
    stop(); start()
    recovered=job_wait(failed["id"],allow_retry=True)
    assert request("GET","/api/report/"+second_new["id"])[0]==200
    assert request("GET","/api/report/"+first_new["id"])[0]==404
    assert not store.catalog.pending_operations()
    mark("fault-injected purge blocks package and resumes from durable PG journal")
    assert request("GET","/api/library")[1]["settings"]["reading"]["lineHeight"]==2
    mark("preferences survive process restart")
    outcome["status"]="PASS"
except Exception as error:
    outcome["status"]="FAIL"; outcome["error"]=repr(error)
    raise
finally:
    stop()
    Path(os.environ["BACKEND_PG_EVIDENCE"]).write_text(json.dumps(outcome,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"status":outcome["status"],"checks":len(outcome["checks"]),"error":outcome.get("error")},ensure_ascii=True))

