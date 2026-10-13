"""Immutable original byte objects. PostgreSQL owns their references and lifecycle."""
from __future__ import annotations
import hashlib, json, os, re, shutil, stat, uuid, zipfile
from pathlib import Path, PurePosixPath

REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
CHUNK = 1024 * 1024

class OriginalError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

def safe_name(name):
    if not isinstance(name, str) or "\x00" in name or "\\" in name or name.startswith("/"):
        raise OriginalError("ZIP 包含不安全路径。")
    parts = name.rstrip("/").split("/")
    for part in parts:
        if (not part or part in (".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', part)
            or part.endswith((" ", ".")) or re.match(r"(?i)^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", part)):
            raise OriginalError("ZIP 包含不安全或系统不支持的路径：" + name)
    return "/".join(parts)

def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    sync_directory(path.parent)

def sync_directory(path):
    # Windows rename durability is provided by file flush plus PG durable intent.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as inp:
        while chunk := inp.read(CHUNK): digest.update(chunk)
    return digest.hexdigest()

class OriginalStore:
    def __init__(self, root, library_id=None):
        if library_id is not None:
            try: library_id=str(uuid.UUID(str(library_id)))
            except (ValueError,TypeError,AttributeError) as exc: raise OriginalError("Library identity is invalid.",409) from exc
        requested = Path(root).absolute()
        self._plain_ancestors(requested)
        self.root = requested.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.checked(self.root / ".reference-library-owner.json")
        if marker.exists():
            try: owner = json.loads(marker.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc: raise OriginalError("Library ownership marker is damaged.", 500) from exc
            if owner.get("owner") != "reference-library" or owner.get("format") != 1 or not re.fullmatch(r"[a-f0-9-]{32,36}", owner.get("libraryId", "")):
                raise OriginalError("Library ownership marker is invalid.", 409)
        else:
            # Never silently claim a preexisting originals tree from another application.
            if (self.root / "originals").exists() and any((self.root / "originals").iterdir()):
                raise OriginalError("An existing originals tree needs an ownership-verified migration.", 409)
            owner = {"format": 1, "owner": "reference-library", "libraryId": library_id or str(uuid.uuid4())}
            atomic_json(marker, owner)
        if library_id is not None and owner["libraryId"] != library_id:
            raise OriginalError("Library root and PostgreSQL configuration identities do not match.",409)
        self.library_id = owner["libraryId"]
        self.originals = self.root / "originals"
        self.staging = self.root / "staging"
        self.journals = self.root / "journals"
        for directory in (self.originals, self.staging, self.journals):
            self._plain_ancestors(directory)
            directory.mkdir(exist_ok=True)
        if self.originals.stat().st_dev != self.staging.stat().st_dev:
            raise OriginalError("暂存和原件必须位于同一文件系统。", 500)

    @staticmethod
    def _plain_ancestors(path):
        for item in (*reversed(Path(path).parents), Path(path)):
            try: info = item.lstat()
            except FileNotFoundError: continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE:
                raise OriginalError("受管理目录中包含链接或重解析点，操作已拒绝。", 409)

    def checked(self, path, base=None, allow_base=False):
        base = Path(base or self.root).absolute()
        path = Path(path).absolute()
        if not path.is_relative_to(base) or (path == base and not allow_base):
            raise OriginalError("路径超出受管理目录。", 409)
        self._plain_ancestors(path)
        if not path.resolve().is_relative_to(base.resolve()):
            raise OriginalError("路径边界验证失败。", 409)
        return path

    def check_tree(self, directory, base=None):
        directory = self.checked(directory, base)
        if not directory.exists(): return
        for current, dirs, files in os.walk(directory, followlinks=False):
            for name in dirs + files: self.checked(Path(current) / name, base)

    @staticmethod
    def object_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{64}", value):
            raise OriginalError("原件对象标识无效。", 409)
        return value

    def object_path(self, object_id):
        return self.checked(self.originals / self.object_id(object_id), self.originals)

    def stage_path(self, stage_id):
        return self.checked(self.staging / self.object_id(stage_id), self.staging)

    def new_stage(self, stage_id=None):
        stage_id = stage_id or uuid.uuid4().hex
        path = self.stage_path(stage_id)
        path.mkdir()
        (path / "files").mkdir()
        return path

    def member_path(self, manifest, name):
        name = safe_name(name)
        # PG member map is authoritative; a guessed file path cannot access auxiliary data.
        files = manifest.get("files", [])
        if not any(member["path"] == name for member in files):
            raise OriginalError("该资源不属于当前资料包。", 404)
        base = self.object_path(manifest["objectId"])
        target = self.checked(base / "files" / Path(*PurePosixPath(name).parts), self.originals)
        if not target.is_file(): raise OriginalError("原件资源缺失，请恢复资料库。", 503)
        return target

    def source_path(self, manifest):
        target = self.checked(self.object_path(manifest["objectId"]) / "source.zip", self.originals)
        if not target.is_file(): raise OriginalError("原始 ZIP 缺失，请恢复资料库。", 503)
        return target

    def promote(self, stage_id, object_id):
        stage = self.stage_path(stage_id)
        target = self.object_path(object_id)
        if target.exists():
            if stage.exists(): raise OriginalError("原件对象已存在，提交已暂停。", 409)
            return target
        self.check_tree(stage, self.staging)
        os.replace(stage, target)
        sync_directory(self.originals)
        return target

    def discard_stage(self, stage_id):
        path = self.stage_path(stage_id)
        self.check_tree(path, self.staging)
        if path.exists(): shutil.rmtree(path)

    def remove_object(self, object_id):
        path = self.object_path(object_id)
        self.check_tree(path, self.originals)
        if path.exists(): shutil.rmtree(path)
        sync_directory(self.originals)

    def manifest_at(self, object_id, stage=False):
        base = self.stage_path(object_id) if stage else self.object_path(object_id)
        path = self.checked(base / "metadata.json", self.staging if stage else self.originals)
        try: result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc: raise OriginalError("原件清单损坏，恢复操作已暂停。", 503) from exc
        if not isinstance(result, dict) or not isinstance(result.get("files"), list):
            raise OriginalError("原件清单无效。", 503)
        return result

    def write_journal(self, operation):
        oid = operation["id"]
        if not isinstance(oid, str) or not re.fullmatch(r"[a-f0-9-]{20,80}", oid):
            raise OriginalError("恢复日志标识无效。", 500)
        atomic_json(self.checked(self.journals / (oid + ".json"), self.journals), operation)

    def delete_journal(self, operation_id):
        if not re.fullmatch(r"[a-f0-9-]{20,80}", operation_id): raise OriginalError("恢复日志标识无效。", 500)
        path = self.checked(self.journals / (operation_id + ".json"), self.journals)
        if path.exists(): path.unlink()

    def _projection_files(self):
        found=set()
        legacy=self.checked(self.root/"evidence"/"legacy")
        if legacy.exists():
            self.check_tree(legacy,self.root)
            for current,dirs,files in os.walk(legacy,followlinks=False):
                for name in files: found.add((Path(current)/name).relative_to(self.root).as_posix())
        for name in ("evidence/legacy-audit.json","evidence/snapshot-manifest.json"):
            if self.checked(self.root/Path(*PurePosixPath(name).parts)).exists(): found.add(name)
        return found

    def _valid_projection_plan(self,plan):
        if not isinstance(plan,dict) or plan.get("format")!=1 or plan.get("owner")!="reference-library-migration-projections" or plan.get("libraryId")!=self.library_id or not re.fullmatch("[a-f0-9]{32}",str(plan.get("projectionId",""))):
            raise OriginalError("迁移投影登记与资料库身份不一致。",409)
        members=plan.get("files")
        if not isinstance(members,list): raise OriginalError("迁移投影登记无效。",409)
        seen=set()
        for member in members:
            name=safe_name(member.get("path"))
            if not (name.startswith("evidence/legacy/") or name in ("evidence/legacy-audit.json","evidence/snapshot-manifest.json")):
                raise OriginalError("迁移投影登记超出允许范围。",409)
            if name.casefold() in seen or type(member.get("size")) is not int or member["size"]<0 or not re.fullmatch("[a-f0-9]{64}",str(member.get("sha256",""))):
                raise OriginalError("迁移投影登记无效。",409)
            seen.add(name.casefold())
        return plan

    def projection_plan(self):
        ledger=self.checked(self.root/".migration-projections.json")
        actual=self._projection_files()
        if not ledger.exists():
            if actual: raise OriginalError("旧迁移投影尚未由外部审计登记，永久删除已拒绝。",409)
            return None
        try: plan=self._valid_projection_plan(json.loads(ledger.read_text(encoding="utf-8-sig")))
        except (OSError,ValueError,TypeError) as exc: raise OriginalError("迁移投影登记损坏，永久删除已拒绝。",409) from exc
        if actual!={item["path"] for item in plan["files"]}:
            raise OriginalError("发现未登记或缺失的迁移投影，永久删除已拒绝。",409)
        for item in plan["files"]:
            path=self.checked(self.root/Path(*PurePosixPath(item["path"]).parts))
            if path.stat().st_size!=item["size"] or hash_file(path)!=item["sha256"]:
                raise OriginalError("迁移投影已发生变化，永久删除已拒绝。",409)
        return {**plan,"ledgerSha256":hash_file(ledger)}

    def clear_projections(self,plan):
        if plan is None: return
        self._valid_projection_plan(plan)
        ledger=self.checked(self.root/".migration-projections.json")
        if ledger.exists() and hash_file(ledger)!=plan.get("ledgerSha256"):
            raise OriginalError("迁移投影登记已变化，删除清理保持封锁。",503)
        registered={item["path"] for item in plan["files"]}
        if self._projection_files()-registered:
            raise OriginalError("发现未知迁移投影，删除清理保持封锁。",503)
        # Validate every remaining file before unlinking any. Missing registered files
        # are accepted only here, under an already durable purge cleanup plan.
        for item in plan["files"]:
            path=self.checked(self.root/Path(*PurePosixPath(item["path"]).parts))
            if path.exists() and (path.stat().st_size!=item["size"] or hash_file(path)!=item["sha256"]):
                raise OriginalError("迁移投影已变化，删除清理保持封锁。",503)
        parents=set()
        for item in plan["files"]:
            path=self.checked(self.root/Path(*PurePosixPath(item["path"]).parts))
            if path.exists(): path.unlink()
            parents.update(parent for parent in path.parents if parent!=self.root and parent.is_relative_to(self.root/"evidence"))
        for parent in sorted(parents,key=lambda p:len(p.parts),reverse=True):
            self.checked(parent)
            if parent.exists():
                try: parent.rmdir()
                except OSError: pass
        if ledger.exists(): ledger.unlink()
        atomic_json(self.checked(self.root/"maintenance-receipt.json"),
            {"format":2,"operation":"library-modified","state":"authority-writes-present",
             "readOnlyAcceptance":False,"libraryId":self.library_id,
             "migrationProjectionCleanup":{"projectionId":plan["projectionId"],"completed":True}})
        sync_directory(self.root)

    def build_survivor(self, old_manifest, new_manifest, keep_paths, stage_id, cancelled=None):
        """Copy exact survivor bytes and construct a ZIP that never contains deleted sources."""
        stage = self.stage_path(stage_id)
        if stage.exists(): self.discard_stage(stage_id)
        stage = self.new_stage(stage_id)
        members = []
        canonical = hashlib.sha256()
        old_members = {m["path"]: m for m in old_manifest["files"]}
        source = self.checked(stage / "source.zip", self.staging)
        with source.open("xb") as out:
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in sorted(keep_paths):
                    if cancelled and cancelled(): raise OriginalError("操作已取消。", 409)
                    member = old_members[name]
                    old = self.member_path(old_manifest, name)
                    destination = self.checked(stage / "files" / Path(*PurePosixPath(name).parts), self.staging)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with old.open("rb") as inp, destination.open("xb") as copied, archive.open(name, "w") as zipped:
                        while chunk := inp.read(CHUNK):
                            digest.update(chunk); size += len(chunk); copied.write(chunk); zipped.write(chunk)
                        copied.flush(); os.fsync(copied.fileno())
                    if digest.hexdigest() != member["sha256"] or size != member["size"]:
                        raise OriginalError("原件哈希变化，永久删除已暂停。", 503)
                    members.append({"path": name, "sha256": digest.hexdigest(), "size": size})
                    canonical.update(name.encode("utf-8")); canonical.update(digest.digest())
            out.flush(); os.fsync(out.fileno())
        manifest = {**new_manifest, "files": members, "currentSha256": hash_file(source),
                    "sha256": hash_file(source), "contentSha256": canonical.hexdigest(),
                    "fileCount": len(members), "expandedBytes": sum(m["size"] for m in members)}
        atomic_json(stage / "metadata.json", manifest)
        return manifest

