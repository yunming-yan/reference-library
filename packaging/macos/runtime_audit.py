from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import stat
import struct
import zipfile

CPU = {0x1000007: "x86_64", 0x100000c: "arm64"}


def version(value):
    return f"{value >> 16}.{(value >> 8) & 255}.{value & 255}"


def macho(data: bytes):
    if data[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        is_64 = data[3] == 0xbf
        count = struct.unpack_from(">I", data, 4)[0]
        out = []
        for index in range(count):
            pos = 8 + index * (32 if is_64 else 20)
            _, _, offset, size = struct.unpack_from(">IIQQ" if is_64 else ">IIII", data, pos)
            out.extend(macho(data[offset:offset + size]))
        return out
    if data[:4] not in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"):
        return []
    endian = "<" if data[0] in (0xcf, 0xce) else ">"
    is_64 = data[0] == 0xcf or data[3] == 0xcf
    _, cpu, subtype, filetype, count = struct.unpack_from(endian + "IIIII", data, 0)
    record = {"architecture": CPU.get(cpu, str(cpu)), "filetype": filetype, "libraries": [], "rpaths": [], "minimumMacOS": None, "codeSignature": False}
    pos = 32 if is_64 else 28
    for _ in range(count):
        command, size = struct.unpack_from(endian + "II", data, pos)
        if size < 8 or pos + size > len(data):
            raise ValueError("Malformed Mach-O load command")
        if command in (0xc, 0xd, 0x18, 0x80000018, 0x1f, 0x8000001f, 0x20, 0x80000020, 0x23, 0x80000023):
            name_offset, _, current, compatibility = struct.unpack_from(endian + "IIII", data, pos + 8)
            name = data[pos + name_offset:pos + size].split(b"\0")[0].decode("utf-8")
            record["libraries"].append({"path": name, "kind": "id" if command == 0xd else "load", "currentVersion": version(current), "compatibilityVersion": version(compatibility)})
        elif command == 0x8000001c:
            name_offset = struct.unpack_from(endian + "I", data, pos + 8)[0]
            record["rpaths"].append(data[pos + name_offset:pos + size].split(b"\0")[0].decode("utf-8"))
        elif command == 0x32:
            platform_id, minos, sdk = struct.unpack_from(endian + "III", data, pos + 8)
            record.update(minimumMacOS=version(minos), sdk=version(sdk), platform=platform_id)
        elif command == 0x24:
            minos, sdk = struct.unpack_from(endian + "II", data, pos + 8)
            record.update(minimumMacOS=version(minos), sdk=version(sdk))
        elif command == 0x1d:
            record["codeSignature"] = True
        pos += size
    return [record]


def audit(archive: Path, output: Path):
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        top = Counter(i.filename.split("/")[0] for i in infos)
        second = Counter("/".join(i.filename.split("/")[:2]) for i in infos)
        symlinks = []
        native = []
        licenses = []
        for info in infos:
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                symlinks.append({"path": info.filename, "target": source.read(info).decode("utf-8"), "mode": oct(mode), "bytes": info.file_size})
            elif not info.is_dir() and info.file_size:
                with source.open(info) as stream:
                    first = stream.read(4)
                    if first in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"):
                        records = macho(first + stream.read())
                        native.append({"path": info.filename, "mode": oct(mode), "slices": records})
                if any(part in info.filename.lower() for part in ("licen", "copying", "copyright", "notice")):
                    licenses.append(info.filename)
    report = {"archive": archive.name, "sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest(), "bytes": archive.stat().st_size, "topLevel": dict(top), "secondLevel": dict(second), "fileCount": len(infos), "uncompressedBytes": sum(i.file_size for i in infos), "symlinks": symlinks, "native": native, "licenseFiles": licenses}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(json.dumps({**{k: report[k] for k in ("topLevel", "secondLevel", "fileCount", "uncompressedBytes")}, "licenseFiles": len(licenses)}))
    print(json.dumps({"symlinkCount": len(symlinks), "nativeCount": len(native), "architectureSets": dict(Counter(",".join(sorted(r["architecture"] for r in n["slices"])) for n in native))}))
    for native_file in native:
        if native_file["path"].startswith(("pgsql/bin/", "pgsql/lib/")) and native_file["path"].split("/")[-1] in ("postgres", "initdb", "pg_ctl", "pg_dump", "pg_restore", "libpq.5.dylib"):
            print(json.dumps(native_file))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    audit(args.archive, args.output)
