"""Build a source-only, allowlisted export. Never initializes Git or publishes."""
from __future__ import annotations
import argparse, ast, hashlib, json, os, re, shutil, stat
from datetime import datetime, timezone
from pathlib import Path

REPARSE=getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0x400)
ROOT_FILES={"README.md","LICENSE",".gitignore","launch.py","打开综合资料库.vbs","关闭综合资料库.vbs"}
APP_FILES={"bootstrap.py","cache_sqlite.py","ingest.py","maintenance.py","original_store.py","pg_catalog.py","runtime_manager.py","server.py"}
TEST_FILES={"test_backend_api.py","test_backend_pg_http.py","test_cache_sqlite.py"}
MAC_FILES={"bootstrap_macos.py","launch_macos.py","server_entry.py","build_app.py","native_smoke.py","build_native_driver.py","launcher.sh","sign_and_notarize.sh","refresh_signed_manifests.py","build_psycopg_c.sh","download_verified.py","download_wheels.py","download_native_build_inputs.py","runtime_audit.py","extract_runtime.py","compare_driver_performance.py","build_native_pg.sh","test_machine_contract.py","native-pg-version-lock.json"}
PROVENANCE_FILES={"release/windows-runtime-dependencies.json","release/psycopg-c-build.json","release/WINDOWS_RUNTIME.md","release/WINDOWS_NATIVE_BUILD.md","release/windows-native-build-lock.json","app/vendor/markdown-manifest.json"}
RUNTIME_PLATFORMS={"windows-x64","macos-universal2"}
WHEEL_PLATFORMS={"windows-x64-cp314","macos-arm64-cp314","macos-x86_64-cp314"}
FORBIDDEN_SUFFIXES={".dll",".exe",".dylib",".so",".a",".lib",".whl",".zip",".gz",".pkg",".dmg",".docx",".png",".jpg",".jpeg",".webm",".mp4",".pyc",".pyo",".prof"}
PRIVATE_PATTERN=re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/]+[\\/]")
DRIVE_PATTERN=re.compile(r"(?i)\b[A-Z]:[\\/]")
SECRET_PATTERN=re.compile(r"(?i)(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|x-amz-(?:credential|signature)=|[\"'](?:password|adminPassword|access_token|refresh_token|client_secret)[\"']\s*:\s*[\"'][^\"']+[\"'])")
SENSITIVE_PARTS={".git",".codex",".agents",".aws","private","data","catalog","originals","staging","journals","state","cache","backups","__pycache__",".venv","venv"}
DROP_METADATA_KEYS={"headers","authorization","cookie","set-cookie","adminPassword","password","privatePath","absolutePath","localPath","machinePath","workingDirectory","venvPath"}

def not_link(path):
    for item in (path,*path.parents):
        try: info=item.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&REPARSE:
            raise ValueError("Links and reparse points are not allowed.")

def digest(path):
    value=hashlib.sha256()
    with path.open("rb") as source:
        while chunk:=source.read(1024*1024):value.update(chunk)
    return value.hexdigest()

def allowed(relative):
    parts=relative.parts; name=parts[-1]
    if any(part.casefold() in SENSITIVE_PARTS for part in parts): return False,"sensitive or disposable directory"
    if relative.suffix.casefold() in FORBIDDEN_SUFFIXES or ".so." in name: return False,"binary or build artifact belongs in platform package"
    if len(parts)==1:return name in ROOT_FILES,"root file is not allowlisted"
    if parts[0]=="app":
        if len(parts)==2:return name in APP_FILES,"legacy app file is not allowlisted"
        if parts[1]=="web":return len(parts)==3 and relative.suffix.casefold() in {".js",".css",".html",".svg"},"only web runtime assets are allowlisted"
        if parts[1]=="vendor":
            if len(parts)==3:return name in {"Markdown-LICENSE.txt","THIRD_PARTY.txt","markdown-manifest.json"},"only vendored license and provenance are allowlisted"
            return parts[2]=="markdown" and relative.suffix==".py","only vendored Markdown source is allowlisted"
    if parts[0]=="schema":return len(parts)==3 and parts[1]=="migrations" and relative.suffix==".sql","only schema migrations are allowlisted"
    if parts[0]=="maintenance":return len(parts)==2 and name in {"__init__.py","README.md"},"only maintained tools and documentation are allowlisted"
    if parts[0]=="tests":return len(parts)==2 and name in TEST_FILES,"only pure synthetic test sources are allowlisted"
    if parts[0]=="docs":return len(parts)==2 and name=="macos.md","only public platform documentation is allowlisted"
    if parts[0]=="packaging":
        if len(parts)==2:return name=="export_public.py","only source export builder is allowlisted"
        if parts[1]=="macos":
            if len(parts)==3:return name in MAC_FILES,"only reviewed build tool sources are allowlisted"
            if len(parts)==4 and parts[2]=="build-support":return name in {"manifest.json","requirements.lock"} or name.endswith(".source.json"),"build-support binaries are excluded"
    if parts[0]=="licenses":
        return relative.suffix.casefold() in {".txt",".md"} or name.upper() in {"LICENSE","COPYING","COPYRIGHT","LICENSE_APACHE"},"binary and placeholder license files are excluded"
    if relative.as_posix() in PROVENANCE_FILES:return True,""
    if parts[0]=="runtime":return len(parts)==4 and parts[1]=="postgresql" and parts[2] in RUNTIME_PLATFORMS and name in {"manifest.json","server_license.txt","commandlinetools_3rd_party_licenses.txt","pgAdmin_license.txt","pgAdmin_3rd_party_licenses.txt","StackBuilder_3rd_party_licenses.txt"},"native runtime payload excluded from Git"
    if parts[0]=="wheels":return len(parts)==3 and parts[1] in WHEEL_PLATFORMS and name in {"manifest.json","requirements.lock"},"wheels excluded from Git"
    return False,"not in source allowlist"

def clean_metadata(value, removed, path=""):
    if isinstance(value,dict):
        result={}
        for key,item in value.items():
            field=path+"."+key if path else key
            if key.casefold() in {name.casefold() for name in DROP_METADATA_KEYS}:
                removed.append(field);continue
            result[key]=clean_metadata(item,removed,field)
        return result
    if isinstance(value,list):return [clean_metadata(item,removed,path+"["+str(i)+"]") for i,item in enumerate(value)]
    return value

def check_text(relative,text,source):
    normalized=text.replace("\\\\","\\").replace("\\","/").casefold()
    dynamic_markers=(Path.home().as_posix().casefold(),source.parent.as_posix().casefold())
    if any(marker and marker in normalized for marker in dynamic_markers):return "private machine or source workspace path"
    user_name=Path.home().name
    if user_name.isdigit() and re.search(r"(?<![0-9])"+re.escape(user_name)+r"(?![0-9])",text):return "current machine user identifier"
    if PRIVATE_PATTERN.search(text):return "private or user-specific absolute path"
    if SECRET_PATTERN.search(text):return "credential material"
    # This one security fixture is deliberately a forbidden drive path, not a user location.
    screened=text.replace("C:/evil.md","") if relative.as_posix() in {"tests/test_backend_api.py","packaging/export_public.py"} else text
    # These exact compile-time prefixes are public logical build examples.
    # DESTDIR staging keeps them separate from every actual machine directory.
    if relative.as_posix() in {"release/WINDOWS_NATIVE_BUILD.md", "release/windows-native-build-lock.json", "runtime/postgresql/windows-x64/manifest.json"}:
        screened = screened.replace("\\", "/")
        for generic in ("C" + ":/OpenSSL", "C" + ":/ReferenceLibrary/PostgreSQL"):
            screened = screened.replace(generic, "")
    # Official third-party licenses may quote generic installation paths; never alter terms.
    license_text=relative.parts[0]=="licenses" or relative.name.endswith("_licenses.txt") or relative.name=="server_license.txt"
    if not license_text and DRIVE_PATTERN.search(screened):return "hardcoded absolute Windows path"
    return None

def walk_files(source):
    for current,dirs,files in os.walk(source,followlinks=False):
        current=Path(current)
        for name in list(dirs):
            target=current/name
            try:not_link(target)
            except ValueError:
                dirs.remove(name);yield target,"link or reparse directory"
            if name in {".git","__pycache__"}:
                dirs.remove(name) if name in dirs else None
                yield target,"history or generated directory not traversed"
        for name in files:yield current/name,None

def export(source,output,evidence):
    source=source.absolute();output=output.absolute();evidence=evidence.absolute()
    not_link(source);not_link(output);not_link(evidence)
    source=source.resolve()
    if output==source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Export destination must be disjoint from the source tree.")
    if evidence==source or evidence.is_relative_to(source) or source.is_relative_to(evidence):
        raise ValueError("Evidence must be disjoint from the source tree.")
    if evidence==output or evidence.is_relative_to(output) or output.is_relative_to(evidence):
        raise ValueError("Evidence must be disjoint from the public export destination.")
    if output.exists() and any(output.iterdir()):raise ValueError("Output must be a new empty directory.")
    output.mkdir(parents=True,exist_ok=True);evidence.mkdir(parents=True,exist_ok=True)
    accepted=[];rejected=[];failures=[];metadata_changes=[]
    for original,pre_rejection in sorted(walk_files(source),key=lambda item:item[0].as_posix()):
        relative=original.relative_to(source)
        permitted,reason=allowed(relative)
        if pre_rejection or not permitted:
            rejected.append({"path":relative.as_posix(),"reason":pre_rejection or reason});continue
        try:
            not_link(original)
            if original.stat().st_size==0:
                rejected.append({"path":relative.as_posix(),"reason":"empty placeholder"});continue
            if original.stat().st_size>16*1024*1024:raise ValueError("Source text exceeds 16 MiB cap.")
            raw=original.read_bytes();source_hash=hashlib.sha256(raw).hexdigest()
            try:text=raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                is_license=relative.parts[0]=="licenses" or relative.name.endswith("_licenses.txt") or relative.name=="server_license.txt"
                if not is_license:raise
                # Scan legacy license encoding without changing its original bytes.
                text=raw.decode("cp1252")
            removed=[]
            if relative.suffix==".json":
                value=clean_metadata(json.loads(text),removed)
                if removed:
                    raw=(json.dumps(value,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
                    text=raw.decode("utf-8");metadata_changes.append({"path":relative.as_posix(),"removedFields":removed})
            issue=check_text(relative,text,source)
            if issue:raise ValueError(issue)
            if relative.suffix==".py":ast.parse(text,filename=relative.as_posix())
            target=output/relative
            not_link(target);target.parent.mkdir(parents=True,exist_ok=True)
            with target.open("xb") as destination:
                destination.write(raw);destination.flush();os.fsync(destination.fileno())
            if digest(original)!=source_hash:raise ValueError("Source changed during export; use a fresh destination and retry.")
            accepted.append({"path":relative.as_posix(),"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"sourceSha256":source_hash,"metadataSanitized":bool(removed)})
        except (ValueError,UnicodeError,OSError,SyntaxError) as exc:
            failures.append({"path":relative.as_posix(),"reason":str(exc)})
    required={"README.md","LICENSE",".gitignore","launch.py","app/server.py","app/ingest.py","app/original_store.py","app/pg_catalog.py","app/runtime_manager.py","app/bootstrap.py","packaging/export_public.py"}
    missing=sorted(required-{item["path"] for item in accepted})
    if missing:failures.append({"path":"<required-files>","reason":"Missing: "+", ".join(missing)})
    combined=hashlib.sha256()
    for item in accepted:combined.update(item["path"].encode("utf-8")+b"\x00"+bytes.fromhex(item["sha256"]))
    manifest={"format":1,"createdAt":datetime.now(timezone.utc).isoformat(),"kind":"source-only-preview","published":False,"gitInitialized":False,"status":"PASS" if not failures else "BLOCKED","files":accepted,"treeSha256":combined.hexdigest(),"fileCount":len(accepted),"totalBytes":sum(item["bytes"] for item in accepted),"rejectedCount":len(rejected),"blockedFiles":failures,"metadataChanges":metadata_changes,"notes":["Platform binaries and wheels are intentionally absent.","No private evidence or machine state is part of this tree.","License notices are retained independently of the project MIT license."]}
    for name,value in (("export-manifest.json",manifest),("rejected-files.json",rejected),("blocked-files.json",failures)):
        (evidence/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (evidence/"tree.txt").write_text("\n".join(item["path"] for item in accepted)+"\n",encoding="utf-8")
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--evidence",type=Path,required=True)
    args=parser.parse_args()
    result=export(args.source,args.output,args.evidence)
    print(json.dumps({key:result[key] for key in ("status","fileCount","totalBytes","rejectedCount","treeSha256")}))
    if result["blockedFiles"]:raise SystemExit(1)

if __name__=="__main__":main()

