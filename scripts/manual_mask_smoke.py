#!/usr/bin/env python3
"""Manual mask API smoke test.

Usage:
  python scripts/manual_mask_smoke.py --base-url http://localhost:8080 --subject sub-001
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


def http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body
        try:
            detail_obj = json.loads(body)
            detail = detail_obj.get("error") or detail_obj.get("detail") or body
        except Exception:
            pass
        raise RuntimeError(f"{method} {url} failed ({exc.code}): {detail}") from exc


def pick_subject(base_url: str, explicit_subject: Optional[str]) -> str:
    if explicit_subject:
        return explicit_subject
    snapshot = http_json("GET", f"{base_url}/api/subjects")
    subjects = sorted((snapshot.get("subjects") or {}).keys())
    if not subjects:
        raise RuntimeError("No subjects found in /api/subjects")
    return subjects[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--subject", default=None)
    parser.add_argument("--preset", default="standard")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    subject = pick_subject(base_url, args.subject)

    print(f"[smoke] Using subject: {subject}")

    # Ensure FastSurfer segmentation/cat availability for this subject.
    _ = http_json("GET", f"{base_url}/api/stl/catalog/{urllib.parse.quote(subject)}")
    print("[smoke] STL catalog reachable")

    try:
        _ = http_json("GET", f"{base_url}/api/mask/catalog/{urllib.parse.quote(subject)}")
    except Exception as exc:
        raise RuntimeError(
            "Manual-mask routes are unavailable on the running API instance. "
            "Restart the web server with the latest workspace code, then rerun this smoke test. "
            f"Details: {exc}"
        ) from exc
    print("[smoke] Manual-mask catalog route reachable")

    init_payload = {
        "preset": args.preset,
        "params": {
            "mask_mode": "manual_smoke",
        },
    }
    init_data = http_json("POST", f"{base_url}/api/mask/init/{urllib.parse.quote(subject)}", init_payload)
    version = (init_data.get("version") or {}).get("version_id")
    if not version:
        raise RuntimeError("init response did not include version_id")
    print(f"[smoke] Init version: {version}")

    ver_data = http_json("GET", f"{base_url}/api/mask/version/{urllib.parse.quote(subject)}/{urllib.parse.quote(version)}")
    shape = ver_data.get("shape") or []
    print(f"[smoke] Version shape: {shape}")

    _ = http_json(
        "GET",
        f"{base_url}/api/mask/slice/{urllib.parse.quote(subject)}/{urllib.parse.quote(version)}?plane=axial&index=0",
    )
    print("[smoke] Slice endpoint returned data")

    anatomy = http_json(
        "GET",
        f"{base_url}/api/mask/anatomy/{urllib.parse.quote(subject)}?axial=0&coronal=0&sagittal=0",
    )
    if not isinstance(anatomy.get("axial", {}).get("data"), list):
        raise RuntimeError("anatomy endpoint did not return axial data")
    print("[smoke] Anatomy endpoint returned orthoview data")

    empty_init_data = http_json(
        "POST",
        f"{base_url}/api/mask/init/{urllib.parse.quote(subject)}",
        {"source_type": "empty"},
    )
    empty_version = (empty_init_data.get("version") or {}).get("version_id")
    if not empty_version:
        raise RuntimeError("empty init response did not include version_id")
    empty_ver_data = http_json(
        "GET",
        f"{base_url}/api/mask/version/{urllib.parse.quote(subject)}/{urllib.parse.quote(empty_version)}",
    )
    if int(empty_ver_data.get("voxel_count") or 0) != 0:
        raise RuntimeError("empty init produced non-zero voxel count")
    print(f"[smoke] Empty init version: {empty_version}")

    save_payload = {
        "parent_version_id": version,
        "allow_branch": True,
        "operations": [
            {
                "type": "grow",
                "iterations": 1,
            },
            {
                "type": "shrink",
                "iterations": 1,
            },
        ],
    }
    save_data = http_json("POST", f"{base_url}/api/mask/version/{urllib.parse.quote(subject)}", save_payload)
    next_version = (save_data.get("version") or {}).get("version_id")
    if not next_version:
        raise RuntimeError("save response did not include new version_id")
    print(f"[smoke] Saved child version: {next_version}")

    stl_data = http_json(
        "POST",
        f"{base_url}/api/stl/from-mask/{urllib.parse.quote(subject)}/{urllib.parse.quote(next_version)}",
        {"preset": "fast_preview"},
    )
    print(f"[smoke] STL job queued: {stl_data.get('job_id')}")

    print("[smoke] Manual mask smoke test passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
