"""Fetch OpenNeuro datasets (or specific participants) into the BIDS data dir.

OpenNeuro stores real file content in a public, unauthenticated S3 bucket
(https://s3.amazonaws.com/openneuro.org/<accession>/...). We list and download with the stdlib
only (no aws CLI / boto3 / datalad dependency). Root-level metadata (participants.tsv,
dataset_description.json, ...) is always fetched; each requested participant's `sub-XX/` tree is
fetched too. Progress is reported through a ``log`` callback.
"""
from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

S3_BASE = "https://s3.amazonaws.com/openneuro.org"
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _list_keys(prefix: str, delimiter: str | None = None) -> list[str]:
    """List object keys under an S3 prefix (handles pagination)."""
    keys: list[str] = []
    token: str | None = None
    while True:
        q: dict[str, str] = {"list-type": "2", "prefix": prefix}
        if delimiter:
            q["delimiter"] = delimiter
        if token:
            q["continuation-token"] = token
        url = f"{S3_BASE}/?{urllib.parse.urlencode(q)}"
        with urllib.request.urlopen(url, timeout=60) as resp:
            root = ET.fromstring(resp.read())
        for c in root.findall(f"{_NS}Contents"):
            key_el = c.find(f"{_NS}Key")
            if key_el is not None and key_el.text:
                keys.append(key_el.text)
        truncated = root.find(f"{_NS}IsTruncated")
        if truncated is not None and truncated.text == "true":
            nxt = root.find(f"{_NS}NextContinuationToken")
            token = nxt.text if nxt is not None else None
            if not token:
                break
        else:
            break
    return keys


def _norm_subjects(participants: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in participants:
        p = str(p).strip()
        if not p:
            continue
        out.append(p if p.startswith("sub-") else f"sub-{p}")
    return out


def fetch_openneuro(
    accession: str,
    participants: Iterable[str],
    dest_dir: str | Path,
    log: Callable[[str], None] = print,
) -> dict:
    """Download an OpenNeuro dataset's root metadata + the given participants into ``dest_dir``.

    Returns a summary dict: {accession, subjects, files, skipped}. Raises on network/listing errors.
    """
    acc = accession.strip().strip("/")
    if not acc:
        raise ValueError("accession is required (e.g. 'ds004796')")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    subs = _norm_subjects(participants)

    # 1) root-level files (participants.tsv, dataset_description.json, README, ...)
    keys = _list_keys(f"{acc}/", delimiter="/")
    # 2) each requested participant's whole tree
    for s in subs:
        sub_keys = _list_keys(f"{acc}/{s}/")
        if not sub_keys:
            log(f"[openneuro] WARNING: no files found for {s} in {acc}")
        keys.extend(sub_keys)

    downloaded = 0
    skipped = 0
    for key in keys:
        rel = key[len(acc) + 1:]  # strip "<acc>/"
        if not rel or key.endswith("/"):
            continue
        out = dest / rel
        if out.exists() and out.stat().st_size > 0:
            skipped += 1
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        url = f"{S3_BASE}/{urllib.parse.quote(key)}"
        urllib.request.urlretrieve(url, out)
        downloaded += 1
        log(f"[openneuro] {rel}")

    summary = {"accession": acc, "subjects": subs, "files": downloaded, "skipped": skipped}
    log(f"[openneuro] fetched {downloaded} file(s), skipped {skipped} existing, for {acc}")
    return summary
