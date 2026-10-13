from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import http.client


class RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.redirects = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects.append({"status": code, "source": req.full_url, "destination": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_json(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ReferenceLibraryBuild/2.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (OSError, http.client.HTTPException):
            if attempt == 2: raise
            time.sleep(1)


def fetch(url: str, destination: Path, expected: str | None = None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = destination.with_suffix(destination.suffix + ".source.json")
    if destination.exists() and metadata_path.exists():
        meta = json.loads(metadata_path.read_text("utf-8"))
        actual = hashlib.file_digest(destination.open("rb"), "sha256").hexdigest()
        if actual == meta["sha256"] and (not expected or actual == expected):
            print(json.dumps({"cached": True, **meta}), flush=True)
            return meta
        raise ValueError("Existing download hash mismatch")
    recorder = RedirectRecorder()
    opener = urllib.request.build_opener(recorder)
    request = urllib.request.Request(url, headers={"User-Agent": "ReferenceLibraryBuild/2.0 (offline redistribution audit)"})
    pending = destination.with_suffix(destination.suffix + ".part")
    digest, total, started = hashlib.sha256(), 0, time.monotonic()
    response = None
    for attempt in range(3):
        try:
            response = opener.open(request, timeout=45)
            break
        except (OSError, http.client.HTTPException):
            if attempt == 2: raise
            time.sleep(1)
    with response, pending.open("wb") as output:
        final_url, headers = response.url, dict(response.headers.items())
        next_report = 32 * 1024 * 1024
        while block := response.read(1024 * 1024):
            output.write(block)
            digest.update(block)
            total += len(block)
            if total >= next_report:
                print(f"downloaded {total} bytes in {time.monotonic()-started:.1f}s", flush=True)
                next_report += 32 * 1024 * 1024
    if expected and digest.hexdigest() != expected:
        raise ValueError("Downloaded file hash does not match source hash")
    os.replace(pending, destination)
    meta = {"sourceUrl": url, "finalUrl": final_url, "redirects": recorder.redirects, "sha256": digest.hexdigest(), "bytes": total, "downloadedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "headers": headers}
    metadata_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(meta), flush=True)
    return meta


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--sha256")
    arguments = parser.parse_args()
    fetch(arguments.url, arguments.destination, arguments.sha256)
