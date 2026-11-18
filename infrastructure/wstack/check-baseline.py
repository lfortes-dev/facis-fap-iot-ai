#!/usr/bin/env python3
"""Check the pinned OCM W-Stack baseline (wstack-images.lock.tsv).

By default only the lock file is checked. --registry also resolves the tags
again (needs skopeo), --cluster compares the pods of a namespace with the
lock (needs kubectl). See README.md.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOCK = Path(__file__).resolve().parent / "wstack-images.lock.tsv"

HEADER_RE = re.compile(
    r"^# deployment @ (?P<commit>[0-9a-f]{7,40}) \(ref (?P<ref>[^)]+)\), "
    r"generated (?P<date>\d{4}-\d{2}-\d{2}T[\d:]+Z)$"
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
KINDS = {"image", "chart"}

# Images the FAP depends on (path without registry host) and minimum version.
# The verifier supports DCQL from 1.3.0.
REQUIRED_IMAGES: dict[str, str | None] = {
    "ocm-wstack/issuance-service": None,
    "ocm-wstack/pre-authorization-bridge": None,
    "ocm-wstack/credential-retrieval-service": None,
    "ocm-wstack/storage-service": None,
    "ocm-wstack/credential-verification-service": "1.3.0",
    "ocm-wstack/status-list-service": None,
    "ocm-wstack/well-known-service": None,
    "tsa/signer": None,
    "tsa/crypto-provider-hashicorp-vault-plugin": None,
}


@dataclass(frozen=True)
class Entry:
    line: int
    kind: str
    source: str
    digest: str
    used_by: str

    @property
    def path(self) -> str:
        no_host = self.source.split("/", 1)[1] if "/" in self.source else self.source
        return re.split(r"[:@]", no_host, maxsplit=1)[0]

    @property
    def tag(self) -> str:
        no_host = self.source.split("/", 1)[1] if "/" in self.source else self.source
        m = re.search(r":([^@]+)", no_host)
        return m.group(1) if m else ""


@dataclass
class Lock:
    commit: str
    ref: str
    generated: str
    entries: list[Entry]


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def print(self, title: str, out=sys.stdout) -> None:
        for m in self.failures:
            print(f"  FAIL {m}", file=out)
        for m in self.warnings:
            print(f"  WARN {m}", file=out)
        state = "FAIL" if self.failures else "ok"
        counts = f"{len(self.failures)} failed, {len(self.warnings)} warnings"
        print(f"{title}: {state} ({counts})", file=out)


def parse_lock(text: str, report: Report) -> Lock | None:
    lines = text.splitlines()
    if not lines:
        report.fail("lock file is empty")
        return None
    m = HEADER_RE.match(lines[0])
    if not m:
        report.fail(
            "line 1: header must read '# deployment @ <commit> (ref <ref>), generated <UTC time>'"
        )
        return None
    if len(lines) < 2 or lines[1].split("\t") != ["kind", "source", "digest", "used_by"]:
        report.fail("line 2: column row must be 'kind<TAB>source<TAB>digest<TAB>used_by'")
        return None
    entries: list[Entry] = []
    for no, raw in enumerate(lines[2:], start=3):
        if not raw.strip() or raw.startswith("#"):
            continue
        cols = raw.split("\t")
        if len(cols) != 4:
            report.fail(f"line {no}: {len(cols)} columns, expected 4")
            continue
        entries.append(Entry(no, *cols))
    return Lock(m["commit"], m["ref"], m["date"], entries)


def version_tuple(tag: str) -> tuple[int, ...] | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(int(x) for x in m.groups()) if m else None


def check_offline(lock: Lock, report: Report, expect_deployment: str | None = None) -> None:
    if expect_deployment and not (
        lock.commit.startswith(expect_deployment) or expect_deployment.startswith(lock.commit)
    ):
        report.fail(f"lock built from deployment {lock.commit}, expected {expect_deployment}")
    if not lock.entries:
        report.fail("no entries")
        return
    seen: dict[tuple[str, str], int] = {}
    for e in lock.entries:
        if e.kind not in KINDS:
            report.fail(f"line {e.line}: kind '{e.kind}' is not one of {sorted(KINDS)}")
        if e.digest == "UNRESOLVED":
            report.fail(f"line {e.line}: {e.source} has no digest (UNRESOLVED)")
        elif not DIGEST_RE.match(e.digest):
            report.fail(f"line {e.line}: {e.source}: digest '{e.digest}' is not sha256:<64 hex>")
        if not e.tag:
            report.fail(f"line {e.line}: {e.source} has no tag")
        if not e.used_by:
            report.warn(f"line {e.line}: {e.source} has no used_by")
        key = (e.kind, e.source)
        if key in seen:
            report.fail(f"line {e.line}: duplicate of line {seen[key]} ({e.kind} {e.source})")
        seen[key] = e.line
    images = {e.path: e for e in lock.entries if e.kind == "image"}
    for path, minimum in REQUIRED_IMAGES.items():
        e = images.get(path)
        if e is None:
            report.fail(f"required image {path} is not in the lock")
            continue
        if minimum:
            have, want = version_tuple(e.tag), version_tuple(minimum)
            if have is None:
                report.fail(f"{path}:{e.tag}: tag is not a version, minimum is {minimum}")
            elif have < want:
                report.fail(f"{path}:{e.tag} is older than the minimum {minimum}")
    for e in lock.entries:
        if e.kind == "image" and version_tuple(e.tag) is None:
            report.warn(f"{e.source}: tag '{e.tag}' is not a version (pinned by digest only)")


def resolve_digest(source: str) -> str | None:
    raw = subprocess.run(
        ["skopeo", "inspect", "--raw", f"docker://{source}"], capture_output=True, timeout=120
    )
    if raw.returncode != 0:
        return None
    dig = subprocess.run(
        ["skopeo", "manifest-digest", "/dev/stdin"],
        input=raw.stdout,
        capture_output=True,
        timeout=30,
    )
    return dig.stdout.decode().strip() if dig.returncode == 0 else None


def check_registry(lock: Lock, report: Report, strict: bool, resolver=resolve_digest) -> None:
    for e in lock.entries:
        if e.kind not in KINDS or not DIGEST_RE.match(e.digest):
            continue
        got = resolver(e.source)
        if got is None:
            report.fail(f"{e.source}: cannot be resolved on its registry")
        elif got != e.digest:
            msg = f"{e.source}: tag now points to {got[:19]}..., lock has {e.digest[:19]}..."
            if strict:
                report.fail(msg)
            else:
                report.warn(msg)


def running_images(namespace: str, context: str | None) -> list[tuple[str, str, str]]:
    cmd = ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
    if context:
        cmd[1:1] = ["--context", context]
    out = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
    rows = []
    for pod in json.loads(out).get("items", []):
        name = pod["metadata"]["name"]
        st = pod.get("status", {})
        for cs in st.get("initContainerStatuses", []) + st.get("containerStatuses", []):
            rows.append((f"{name}/{cs['name']}", cs.get("image", ""), cs.get("imageID", "")))
    return rows


def image_path(ref: str) -> str:
    first, _, rest = ref.partition("/")
    no_host = rest if rest and ("." in first or ":" in first or first == "localhost") else ref
    return re.split(r"[:@]", no_host, maxsplit=1)[0]


def check_cluster(lock: Lock, report: Report, rows: list[tuple[str, str, str]]) -> None:
    pinned: dict[str, set[str]] = {}
    for e in lock.entries:
        if e.kind == "image":
            pinned.setdefault(e.path, set()).add(e.digest)
    if not rows:
        report.fail("no running containers found in the namespace")
        return
    for who, image, image_id in rows:
        path = image_path(image)
        digest = image_id.rsplit("@", 1)[1] if "@" in image_id else ""
        if path not in pinned:
            report.warn(f"{who}: {image} is not in the lock")
        elif digest not in pinned[path]:
            pins = ", ".join(d[:19] for d in sorted(pinned[path]))
            running = digest[:19] or "an unknown digest"
            report.fail(f"{who}: {image} runs {running}, lock pins {pins}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument(
        "--expect-deployment",
        metavar="SHA",
        help="commit of eclipse-xfsc/deployment the lock must come from",
    )
    ap.add_argument("--registry", action="store_true", help="re-resolve every tag (skopeo)")
    ap.add_argument("--strict", action="store_true", help="with --registry: drift fails")
    ap.add_argument(
        "--cluster",
        metavar="NAMESPACE",
        help="compare the pods of NAMESPACE with the lock (kubectl, read-only)",
    )
    ap.add_argument("--context", help="kubectl context for --cluster")
    args = ap.parse_args(argv)

    if not args.lock.is_file():
        print(f"lock file not found: {args.lock}", file=sys.stderr)
        return 2
    report = Report()
    lock = parse_lock(args.lock.read_text(), report)
    if lock is None:
        report.print("baseline (lock file)")
        return 1
    print(
        f"lock: {args.lock.name}, deployment @ {lock.commit} (ref {lock.ref}), "
        f"generated {lock.generated}, {len(lock.entries)} entries"
    )
    check_offline(lock, report, args.expect_deployment)
    report.print("baseline (lock file)")
    failed = bool(report.failures)

    if args.registry:
        if not shutil.which("skopeo"):
            print("--registry needs skopeo", file=sys.stderr)
            return 2
        r = Report()
        check_registry(lock, r, args.strict)
        r.print("baseline (registry)")
        failed |= bool(r.failures)

    if args.cluster:
        if not shutil.which("kubectl"):
            print("--cluster needs kubectl", file=sys.stderr)
            return 2
        r = Report()
        try:
            rows = running_images(args.cluster, args.context)
        except subprocess.CalledProcessError as exc:
            print(f"kubectl failed: {exc.stderr.decode().strip()}", file=sys.stderr)
            return 1
        check_cluster(lock, r, rows)
        r.print(f"baseline (cluster, namespace {args.cluster})")
        failed |= bool(r.failures)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
