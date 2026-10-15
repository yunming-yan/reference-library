"""Build-time preparation of two original MiSans assets for the complete app.

The public source intentionally excludes MiSans raw font files. This tool uses
only the pinned official Xiaomi archive and validates the complete font manifest,
central-directory CRC, original sizes and SHA256. It never runs at app startup,
installs a system font, replaces a different existing file or publishes a font.
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os
from pathlib import Path
import struct, sys, tempfile, urllib.request, zlib

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('reference_library_source_export',HERE/'export_public.py')
exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
OFFICIAL_ARCHIVE='https://hyperos.mi.com/font-download/MiSans.zip'
ARCHIVE_BYTES=227880072
MAX_RANGE_BYTES=16_000_000

class OfficialOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if newurl!=OFFICIAL_ARCHIVE:raise ValueError('Refusing unreviewed archive redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def ranged(start,end):
    if not isinstance(start,int) or not isinstance(end,int) or not 0<=start<=end<ARCHIVE_BYTES:
        raise ValueError('Invalid official archive byte range')
    length=end-start+1
    if length>MAX_RANGE_BYTES:raise ValueError('Official archive range exceeds fixed cap')
    request=urllib.request.Request(OFFICIAL_ARCHIVE,headers={
        'User-Agent':'ReferenceLibrary-ApplicationFontBuild/1.0',
        'Accept-Encoding':'identity','Range':f'bytes={start}-{end}'})
    with urllib.request.build_opener(OfficialOnlyRedirect()).open(request,timeout=45) as response:
        if response.status!=206 or response.geturl()!=OFFICIAL_ARCHIVE:
            raise ValueError('Official archive must return the requested partial content')
        if response.headers.get('Content-Range')!=f'bytes {start}-{end}/{ARCHIVE_BYTES}':
            raise ValueError('Official archive Content-Range changed')
        if response.headers.get('Content-Encoding') not in (None,'identity'):
            raise ValueError('Unexpected archive content encoding')
        raw=response.read(length+1)
    if len(raw)!=length:raise ValueError('Official archive range byte count mismatch')
    return raw

def directory():
    start=ARCHIVE_BYTES-65557;tail=ranged(start,ARCHIVE_BYTES-1)
    pos=tail.rfind(b'PK\x05\x06')
    if pos<0 or pos+22>len(tail):raise ValueError('Official ZIP directory footer missing')
    end=struct.unpack_from('<4s4H2LH',tail,pos)
    _,disk,central_disk,disk_count,count,size,offset,comment=end
    if disk or central_disk or disk_count!=count or not 1<=count<=10000 or pos+22+comment!=len(tail):
        raise ValueError('Unexpected official ZIP directory structure')
    if not 0<size<=4_000_000 or offset+size>start+pos:raise ValueError('Official ZIP directory outside archive')
    data=tail[offset-start:offset-start+size] if start<=offset and offset+size<=ARCHIVE_BYTES else ranged(offset,offset+size-1)
    records={};cursor=0
    for _ in range(count):
        if cursor+46>len(data) or data[cursor:cursor+4]!=b'PK\x01\x02':raise ValueError('Invalid official ZIP central entry')
        x=struct.unpack_from('<4s6H3L5H2L',data,cursor);nl,el,cl=x[10:13]
        next_cursor=cursor+46+nl+el+cl
        if next_cursor>len(data):raise ValueError('Truncated official ZIP central entry')
        name=data[cursor+46:cursor+46+nl].decode('utf-8' if x[3]&0x800 else 'cp437')
        if name in records:raise ValueError('Duplicate official ZIP member')
        records[name]={'flags':x[3],'method':x[4],'crc':x[7],'compressed':x[8],'bytes':x[9],'offset':x[16]}
        cursor=next_cursor
    if cursor!=len(data):raise ValueError('Unexpected trailing ZIP directory bytes')
    return records,offset

def extract(record,expected,central_offset):
    if record['flags']&1 or record['method'] not in (0,8):raise ValueError('Encrypted or unsupported official ZIP member')
    if record['bytes']!=expected['bytes'] or record['crc']!=int(expected['zipCRC32'],16):raise ValueError('Official ZIP central CRC/size changed')
    if not 0<record['compressed']<MAX_RANGE_BYTES-65536:raise ValueError('Official font compressed member exceeds cap')
    offset=record['offset'];head=ranged(offset,offset+29)
    x=struct.unpack('<4s5H3L2H',head)
    if x[0]!=b'PK\x03\x04' or x[2]&1 or x[3]!=record['method']:raise ValueError('Official ZIP local header mismatch')
    header_length=30+x[9]+x[10];end=offset+header_length+record['compressed']
    if end>central_offset:raise ValueError('Official font payload overlaps ZIP directory')
    packed=ranged(offset,end-1)
    name=packed[30:30+x[9]].decode('utf-8' if x[2]&0x800 else 'cp437')
    if name!=expected['zipMember']:raise ValueError('Official ZIP local member name changed')
    compressed=packed[header_length:]
    # Authoritative size/CRC come from the central directory; local headers may
    # legitimately be zero when the official ZIP uses data descriptors.
    if record['method']==8:
        decoder=zlib.decompressobj(-15);raw=decoder.decompress(compressed,expected['bytes']+1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('Invalid or oversized original font deflate stream')
    else:raw=compressed
    if len(raw)!=expected['bytes'] or zlib.crc32(raw)&0xffffffff!=int(expected['zipCRC32'],16):
        raise ValueError('Original font CRC/size mismatch')
    if hashlib.sha256(raw).hexdigest()!=expected['sha256']:raise ValueError('Original font SHA256 mismatch')
    exporter.check_font_binary(expected['path'],raw,exporter.FONT_ASSET_RECORDS[expected['path']])
    return raw

def valid_existing(path,record):
    exporter.not_link(path)
    if not path.exists():return False
    if not path.is_file() or path.stat().st_size!=record['bytes'] or exporter.digest(path)!=record['sha256']:
        raise ValueError('Existing font differs; it was preserved. Move it aside deliberately before rebuilding.')
    return True

def atomic_install(root,record,raw):
    relative=exporter.font_asset_path(record['path']);target=root/relative
    if valid_existing(target,record):return 'existing-exact'
    exporter.not_link(target.parent);target.parent.mkdir(parents=True,exist_ok=True)
    exporter.not_link(target.parent)
    fd,temp=tempfile.mkstemp(prefix='.'+target.name+'.preparing-',dir=target.parent)
    temp=Path(temp)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        if exporter.digest(temp)!=record['sha256']:raise ValueError('Prepared font temporary hash mismatch')
        # Link creation atomically refuses an existing destination on Windows
        # and macOS. Unlinking only this owned temporary leaves a normal file.
        try:os.link(temp,target)
        except FileExistsError:
            if valid_existing(target,record):return 'concurrent-existing-exact'
            raise
        if not valid_existing(target,record):raise ValueError('Installed font validation failed')
        return 'prepared-original'
    finally:
        if temp.exists():temp.unlink()

def prepare(root,verify_only=False):
    root=Path(root).absolute();exporter.not_link(root)
    if not root.is_dir():raise ValueError('Application source root missing')
    manifest=exporter.check_font_inputs(root,'source')
    family=next(x for x in manifest['families'] if x['id']=='misans')
    if family['sourceUrl']!=OFFICIAL_ARCHIVE or family['distributionPolicy']!='embedded-app-only':raise ValueError('Official MiSans acquisition policy changed')
    expected=family['files'];missing=[x for x in expected if not valid_existing(root/exporter.font_asset_path(x['path']),x)]
    if missing and verify_only:raise ValueError('Original MiSans assets are not prepared')
    records,central_offset=directory() if missing else ({},0)
    result=[]
    for item in expected:
        if valid_existing(root/exporter.font_asset_path(item['path']),item):status='existing-exact'
        else:
            if item['zipMember'] not in records:raise ValueError('Required official MiSans ZIP member missing')
            raw=extract(records[item['zipMember']],item,central_offset)
            status=atomic_install(root,item,raw)
        result.append({'path':item['path'],'bytes':item['bytes'],'sha256':item['sha256'],'status':status})
    exporter.check_font_inputs(root,'application')
    return {'status':'PASS','scope':'build-time complete application preparation only','networkUsed':bool(missing),
        'runtimeNetworkRequired':False,'fontManifestSha256':exporter.FONT_MANIFEST_SHA256,'fontFiles':result}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=HERE.parent,help='Application source root; only exact manifest destinations are written')
    p.add_argument('--verify-only',action='store_true',help='Verify prepared original assets without network or writes')
    a=p.parse_args();print(json.dumps(prepare(a.root,a.verify_only),ensure_ascii=False),flush=True)
if __name__=='__main__':
    try:main()
    except (ValueError,OSError,UnicodeError,KeyError,struct.error,zlib.error) as exc:
        print(json.dumps({'status':'BLOCKED','errorType':type(exc).__name__,'message':str(exc)},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)