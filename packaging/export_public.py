"""Build a source-only, allowlisted export. Never initializes Git or publishes."""
from __future__ import annotations
import argparse, ast, hashlib, json, os, re, shutil, stat, struct
from datetime import datetime, timezone
from pathlib import Path

REPARSE=getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0x400)
ROOT_FILES={"README.md","LICENSE",".gitignore","launch.py","打开综合资料库.vbs","关闭综合资料库.vbs"}
APP_FILES={"bootstrap.py","cache_sqlite.py","ingest.py","maintenance.py","original_store.py","pg_catalog.py","runtime_manager.py","server.py","reading_preferences.py"}
TEST_FILES={"test_backend_api.py","test_backend_pg_http.py","test_cache_sqlite.py","test_listing_pg_cache.py","test_platform_directories.py","run_source_validation.py","source-ci-requirements.txt","test_reading_preferences.py"}
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

# Reviewed original application resources. No suffix-wide binary allowlist.
FONT_MANIFEST_SHA256="bcb501c6c3d6b708658b7756c6db47e6524023f11903c4c55ad9076cb02be00a"
FONT_ASSET_RECORDS={'app/web/fonts/chill-round/Bold.ttf': {'bytes': 7004128,
                                        'distributionPolicy': 'bundled-source-and-app',
                                        'family': 'chill-round',
                                        'format': 'truetype',
                                        'role': 'font',
                                        'sha256': 'f2c4f9295d9d04a1eb6392ae3c51ecf2a9ffab355a45c6686eeec5eede3381ec'},
 'app/web/fonts/chill-round/LICENSE.txt': {'bytes': 4375,
                                           'distributionPolicy': 'license-and-metadata-source-and-app',
                                           'role': 'font-license-notice',
                                           'sha256': 'd45891f8adfd21368c98e803603df1f575ff3fc4a6ec713ba7ccf0e3cba15b28'},
 'app/web/fonts/chill-round/NOTICE.txt': {'bytes': 679,
                                          'distributionPolicy': 'license-and-metadata-source-and-app',
                                          'role': 'font-license-notice',
                                          'sha256': '6feb76c46ddfa7b6b856ff0f76b3ab6bfdf62e9f110b8b533fb51f63dcab8855'},
 'app/web/fonts/chill-round/Regular.ttf': {'bytes': 6205268,
                                           'distributionPolicy': 'bundled-source-and-app',
                                           'family': 'chill-round',
                                           'format': 'truetype',
                                           'role': 'font',
                                           'sha256': '7ccfc2cd0e971edeac7f65516df8db379f0f84d0d8ff79ee4dfe377956418d3b'},
 'app/web/fonts/lxgw-wenkai-screen/LICENSE.txt': {'bytes': 4455,
                                                  'distributionPolicy': 'license-and-metadata-source-and-app',
                                                  'role': 'font-license-notice',
                                                  'sha256': '4d72e3080cdb3be63638bb7197c7caae55b6e4cbe1bab1ded2830bdff0c9b438'},
 'app/web/fonts/lxgw-wenkai-screen/NOTICE.txt': {'bytes': 807,
                                                 'distributionPolicy': 'license-and-metadata-source-and-app',
                                                 'role': 'font-license-notice',
                                                 'sha256': '9a57023fa6783628c205712024797d36efd2647ba41c58a6a9aa1735e2ddca39'},
 'app/web/fonts/lxgw-wenkai-screen/Regular.ttf': {'bytes': 25673994,
                                                  'distributionPolicy': 'bundled-source-and-app',
                                                  'family': 'lxgw-wenkai',
                                                  'format': 'truetype',
                                                  'role': 'font',
                                                  'sha256': 'cd1a6fa39c4ea42fd8f4e289945789b0e510cf7016435640f8893cdad9b220f3'},
 'app/web/fonts/manifest.json': {'bytes': 13633,
                                 'distributionPolicy': 'license-and-metadata-source-and-app',
                                 'role': 'font-manifest',
                                 'sha256': 'bcb501c6c3d6b708658b7756c6db47e6524023f11903c4c55ad9076cb02be00a'},
 'app/web/fonts/misans/Bold.woff2': {'bytes': 5081104,
                                     'distributionPolicy': 'embedded-app-only',
                                     'family': 'misans',
                                     'format': 'woff2',
                                     'role': 'font',
                                     'sha256': '1c5a7515b61bc82baaa2e2c2fdae2032479fb9a99e09d4d021dc17314fc5939b'},
 'app/web/fonts/misans/LICENSE.pdf': {'bytes': 79535,
                                      'distributionPolicy': 'license-and-metadata-source-and-app',
                                      'role': 'font-license-document',
                                      'sha256': '4a93a27cd2bd81b3b5ecfd0a853144a876fa26938a93a68443c67d74172fcb86'},
 'app/web/fonts/misans/LICENSE.txt': {'bytes': 9942,
                                      'distributionPolicy': 'license-and-metadata-source-and-app',
                                      'role': 'font-license-notice',
                                      'sha256': 'ce325fc07eb389826e90647dfcf83d65d890fb5734c3199f5c87e72a4cea2018'},
 'app/web/fonts/misans/NOTICE.txt': {'bytes': 850,
                                     'distributionPolicy': 'license-and-metadata-source-and-app',
                                     'role': 'font-license-notice',
                                     'sha256': '5001d72821634acae01fa477ef335f7a6daabee48d62f421c16df5b9601adec3'},
 'app/web/fonts/misans/Regular.woff2': {'bytes': 4858624,
                                        'distributionPolicy': 'embedded-app-only',
                                        'family': 'misans',
                                        'format': 'woff2',
                                        'role': 'font',
                                        'sha256': 'd704c1a932c0bd7e8a071d276cd81c0ed0c9fecfa26ac234f4bed0559fe1cb2d'},
 'app/web/fonts/noto-sans-sc/LICENSE.txt': {'bytes': 4388,
                                            'distributionPolicy': 'license-and-metadata-source-and-app',
                                            'role': 'font-license-notice',
                                            'sha256': '1c05c68c34f9708415aada51f17e1b0092d2cea709bf4a94cd38114f9e73d7d9'},
 'app/web/fonts/noto-sans-sc/NOTICE.txt': {'bytes': 762,
                                           'distributionPolicy': 'license-and-metadata-source-and-app',
                                           'role': 'font-license-notice',
                                           'sha256': '5451bd61d9c37f1f7116266acc7cc1035cc504cd7a58812d3df8d12a0fb4c41e'},
 'app/web/fonts/noto-sans-sc/Variable.ttf': {'bytes': 17772300,
                                             'distributionPolicy': 'bundled-source-and-app',
                                             'family': 'noto-sans-sc',
                                             'format': 'truetype',
                                             'role': 'font',
                                             'sha256': 'a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da'},
 'licenses/fonts/chill-round/LICENSE.txt': {'bytes': 4375,
                                            'distributionPolicy': 'license-and-metadata-source-and-app',
                                            'role': 'font-license-notice',
                                            'sha256': 'd45891f8adfd21368c98e803603df1f575ff3fc4a6ec713ba7ccf0e3cba15b28'},
 'licenses/fonts/chill-round/NOTICE.txt': {'bytes': 679,
                                           'distributionPolicy': 'license-and-metadata-source-and-app',
                                           'role': 'font-license-notice',
                                           'sha256': '6feb76c46ddfa7b6b856ff0f76b3ab6bfdf62e9f110b8b533fb51f63dcab8855'},
 'licenses/fonts/lxgw-wenkai-screen/LICENSE.txt': {'bytes': 4455,
                                                   'distributionPolicy': 'license-and-metadata-source-and-app',
                                                   'role': 'font-license-notice',
                                                   'sha256': '4d72e3080cdb3be63638bb7197c7caae55b6e4cbe1bab1ded2830bdff0c9b438'},
 'licenses/fonts/lxgw-wenkai-screen/NOTICE.txt': {'bytes': 807,
                                                  'distributionPolicy': 'license-and-metadata-source-and-app',
                                                  'role': 'font-license-notice',
                                                  'sha256': '9a57023fa6783628c205712024797d36efd2647ba41c58a6a9aa1735e2ddca39'},
 'licenses/fonts/misans/LICENSE.pdf': {'bytes': 79535,
                                       'distributionPolicy': 'license-and-metadata-source-and-app',
                                       'role': 'font-license-document',
                                       'sha256': '4a93a27cd2bd81b3b5ecfd0a853144a876fa26938a93a68443c67d74172fcb86'},
 'licenses/fonts/misans/LICENSE.txt': {'bytes': 9942,
                                       'distributionPolicy': 'license-and-metadata-source-and-app',
                                       'role': 'font-license-notice',
                                       'sha256': 'ce325fc07eb389826e90647dfcf83d65d890fb5734c3199f5c87e72a4cea2018'},
 'licenses/fonts/misans/NOTICE.txt': {'bytes': 850,
                                      'distributionPolicy': 'license-and-metadata-source-and-app',
                                      'role': 'font-license-notice',
                                      'sha256': '5001d72821634acae01fa477ef335f7a6daabee48d62f421c16df5b9601adec3'},
 'licenses/fonts/noto-sans-sc/LICENSE.txt': {'bytes': 4388,
                                             'distributionPolicy': 'license-and-metadata-source-and-app',
                                             'role': 'font-license-notice',
                                             'sha256': '1c05c68c34f9708415aada51f17e1b0092d2cea709bf4a94cd38114f9e73d7d9'},
 'licenses/fonts/noto-sans-sc/NOTICE.txt': {'bytes': 762,
                                            'distributionPolicy': 'license-and-metadata-source-and-app',
                                            'role': 'font-license-notice',
                                            'sha256': '5451bd61d9c37f1f7116266acc7cc1035cc504cd7a58812d3df8d12a0fb4c41e'},
 'release/bundled-fonts.json': {'bytes': 13633,
                                'distributionPolicy': 'license-and-metadata-source-and-app',
                                'role': 'font-manifest',
                                'sha256': 'bcb501c6c3d6b708658b7756c6db47e6524023f11903c4c55ad9076cb02be00a'}}
EMBEDDED_ONLY_FONT_PATHS=frozenset(p for p,r in FONT_ASSET_RECORDS.items() if r["distributionPolicy"]=="embedded-app-only")

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

def allowed(relative,distribution="source"):
    parts=relative.parts; name=parts[-1]
    if any(part.casefold() in SENSITIVE_PARTS for part in parts): return False,"sensitive or disposable directory"
    if relative.as_posix() in FONT_ASSET_RECORDS:
        if relative.as_posix() in EMBEDDED_ONLY_FONT_PATHS and distribution=="source":return False,"MiSans original bytes are embedded-app-only; use prepare_misans then application export"
        return True,""
    if relative.suffix.casefold() in FORBIDDEN_SUFFIXES or ".so." in name: return False,"binary or build artifact belongs in platform package"
    if relative.as_posix()==".github/workflows/source-validation.yml":return True,""
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
    if parts[0]=="docs":return len(parts)==2 and name in {"macos.md","fonts.md"},"only public platform documentation is allowlisted"
    if parts[0]=="packaging":
        if len(parts)==2:return name in {"export_public.py","prepare_misans.py","build_windows_release.py"},"only reviewed export and build helpers are allowlisted"
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

def font_asset_path(name):
    if not isinstance(name,str) or "\\" in name or "\x00" in name:
        raise ValueError("Malformed reviewed font asset path")
    p=Path(name)
    if p.is_absolute() or p.as_posix()!=name or any(x in {"",".",".."} or ":" in x for x in p.parts):
        raise ValueError("Unsafe reviewed font asset path")
    if name not in FONT_ASSET_RECORDS:raise ValueError("Font asset is outside the reviewed exact allowlist")
    return p

def check_font_binary(name,raw,record):
    if len(raw)!=record["bytes"] or hashlib.sha256(raw).hexdigest()!=record["sha256"]:
        raise ValueError("Reviewed font asset hash/size mismatch: "+name)
    if not 0<len(raw)<100_000_000:raise ValueError("Reviewed font asset exceeds the below-100MB cap")
    if record.get("format")=="woff2":
        if len(raw)<48 or raw[:4]!=b"wOF2" or struct.unpack_from(">I",raw,8)[0]!=len(raw):raise ValueError("Invalid approved WOFF2 header")
        if not 1<=struct.unpack_from(">H",raw,12)[0]<=256:raise ValueError("Invalid approved WOFF2 table count")
    elif record.get("format")=="truetype":
        if len(raw)<12 or raw[:4]!=b"\x00\x01\x00\x00":raise ValueError("Invalid approved TrueType signature")
        count=struct.unpack_from(">H",raw,4)[0]
        if not 1<=count<=256 or 12+count*16>len(raw):raise ValueError("Invalid approved TrueType directory")
        for i in range(count):
            _,_,offset,length=struct.unpack_from(">4sIII",raw,12+i*16)
            if offset>len(raw) or length>len(raw)-offset:raise ValueError("Approved TrueType table outside font")
    elif record["role"]=="font-license-document" and not raw.startswith(b"%PDF-"):
        raise ValueError("Invalid reviewed license PDF")

def check_binary_privacy(raw,source):
    # Raw original assets have independently verified official provenance. Also
    # screen ASCII/UTF-16 machine paths and credential markers without rewriting.
    needles={str(Path.home()),str(source.parent)}
    for encoding in ("latin1","utf-16-le","utf-16-be"):
        text=raw.decode(encoding,errors="ignore")
        normal=text.replace("\\","/").casefold()
        if PRIVATE_PATTERN.search(text) or SECRET_PATTERN.search(text):raise ValueError("Private path or credential material in reviewed binary asset")
        if any(x.replace("\\","/").casefold() in normal for x in needles if len(x)>3):raise ValueError("Private machine root in reviewed binary asset")

def check_font_inputs(source,distribution):
    if distribution not in {"source","application"}:raise ValueError("Invalid export distribution target")
    for name in ("release/bundled-fonts.json","app/web/fonts/manifest.json"):
        path=source/font_asset_path(name);not_link(path)
        if not path.is_file() or digest(path)!=FONT_MANIFEST_SHA256:raise ValueError("Approved complete font manifest missing or changed")
    manifest=json.loads((source/"release/bundled-fonts.json").read_text("utf-8"))
    if {x["id"] for x in manifest["families"]}!={"misans","noto-sans-sc","lxgw-wenkai","chill-round"}:raise ValueError("Approved font family set changed")
    for name,record in FONT_ASSET_RECORDS.items():
        path=source/font_asset_path(name);not_link(path)
        if not path.exists() and name in EMBEDDED_ONLY_FONT_PATHS and distribution=="source":continue
        if not path.is_file() or path.stat().st_size!=record["bytes"] or digest(path)!=record["sha256"]:
            raise ValueError("Required approved font/complete license missing or changed: "+name)
        if record["role"] in {"font","font-license-document"}:
            raw=path.read_bytes();check_font_binary(name,raw,record);check_binary_privacy(raw,source)
    return manifest

def export(source,output,evidence,*,distribution="source"):
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
    check_font_inputs(source,distribution)
    output.mkdir(parents=True,exist_ok=True);evidence.mkdir(parents=True,exist_ok=True)
    accepted=[];rejected=[];failures=[];metadata_changes=[]
    for original,pre_rejection in sorted(walk_files(source),key=lambda item:item[0].as_posix()):
        relative=original.relative_to(source)
        permitted,reason=allowed(relative,distribution)
        if pre_rejection or not permitted:
            rejected.append({"path":relative.as_posix(),"reason":pre_rejection or reason});continue
        try:
            not_link(original)
            if original.stat().st_size==0:
                rejected.append({"path":relative.as_posix(),"reason":"empty placeholder"});continue
            approved=FONT_ASSET_RECORDS.get(relative.as_posix())
            binary=bool(approved and approved["role"] in {"font","font-license-document"})
            cap=100_000_000-1 if binary else 16*1024*1024
            if original.stat().st_size>cap:raise ValueError("Approved asset or source text exceeds its size cap.")
            raw=original.read_bytes();source_hash=hashlib.sha256(raw).hexdigest();removed=[]
            if binary:
                check_font_binary(relative.as_posix(),raw,approved);check_binary_privacy(raw,source)
            else:
                try:text=raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    is_license=relative.parts[0]=="licenses" or relative.name.endswith("_licenses.txt") or relative.name=="server_license.txt"
                    if not is_license:raise
                    text=raw.decode("cp1252")
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
            accepted.append({"path":relative.as_posix(),"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"sourceSha256":source_hash,"metadataSanitized":bool(removed),"reviewedApplicationAsset":bool(approved)})
        except (ValueError,UnicodeError,OSError,SyntaxError) as exc:
            failures.append({"path":relative.as_posix(),"reason":str(exc)})
    required={"README.md","LICENSE",".gitignore","launch.py","app/server.py","app/ingest.py","app/original_store.py","app/pg_catalog.py","app/runtime_manager.py","app/bootstrap.py","packaging/export_public.py"}
    required.update({".github/workflows/source-validation.yml","tests/run_source_validation.py","tests/source-ci-requirements.txt","tests/test_listing_pg_cache.py","tests/test_platform_directories.py"})
    required.update({"app/reading_preferences.py","tests/test_reading_preferences.py","docs/fonts.md","packaging/prepare_misans.py","packaging/build_windows_release.py"})
    required.update(name for name in FONT_ASSET_RECORDS if distribution=="application" or name not in EMBEDDED_ONLY_FONT_PATHS)
    missing=sorted(required-{item["path"] for item in accepted})
    if missing:failures.append({"path":"<required-files>","reason":"Missing: "+", ".join(missing)})
    combined=hashlib.sha256()
    for item in accepted:combined.update(item["path"].encode("utf-8")+b"\x00"+bytes.fromhex(item["sha256"]))
    manifest={"format":1,"createdAt":datetime.now(timezone.utc).isoformat(),"kind":"source-only-preview","published":False,"gitInitialized":False,"status":"PASS" if not failures else "BLOCKED","files":accepted,"treeSha256":combined.hexdigest(),"fileCount":len(accepted),"totalBytes":sum(item["bytes"] for item in accepted),"rejectedCount":len(rejected),"blockedFiles":failures,"metadataChanges":metadata_changes,"notes":["Platform binaries and wheels are intentionally absent.","No private evidence or machine state is part of this tree.","License notices are retained independently of the project MIT license."]}
    manifest.update(distributionTarget=distribution,fontManifestSha256=FONT_MANIFEST_SHA256,reviewedApplicationAssets=[dict(record,path=name) for name,record in FONT_ASSET_RECORDS.items() if distribution=="application" or name not in EMBEDDED_ONLY_FONT_PATHS],omittedEmbeddedApplicationAssets=[dict(FONT_ASSET_RECORDS[name],path=name) for name in sorted(EMBEDDED_ONLY_FONT_PATHS)] if distribution=="source" else [],buildPreparation="packaging/prepare_misans.py before application export; never at application runtime")
    manifest["notes"].append("Approved original font resources and complete licenses are application assets. Default source excludes MiSans raw bytes; application distribution requires and includes all six font files.")
    for name,value in (("export-manifest.json",manifest),("rejected-files.json",rejected),("blocked-files.json",failures)):
        (evidence/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (evidence/"tree.txt").write_text("\n".join(item["path"] for item in accepted)+"\n",encoding="utf-8")
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--evidence",type=Path,required=True)
    parser.add_argument("--distribution",choices=("source","application"),default="source",help="source excludes MiSans raw assets; application requires all six fonts")
    args=parser.parse_args()
    result=export(args.source,args.output,args.evidence,distribution=args.distribution)
    print(json.dumps({key:result[key] for key in ("status","fileCount","totalBytes","rejectedCount","treeSha256")}))
    if result["blockedFiles"]:raise SystemExit(1)

if __name__=="__main__":main()

