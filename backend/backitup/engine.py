"""Git and archive operations. Repository code is never checked out or executed."""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import zstandard

from backitup.errors import BackItUpError


@dataclass
class Budget:
    root: Path
    max_bytes: int = 10_000_000_000
    deadline: float = field(default_factory=lambda: time.monotonic() + 3600)
    cancelled: threading.Event = field(default_factory=threading.Event)
    _last_disk_check: float = 0

    def check(self, *, disk: bool = True) -> None:
        if self.cancelled.is_set():
            raise BackItUpError("cancelled", "Job was cancelled")
        if time.monotonic() >= self.deadline:
            raise BackItUpError("job_timeout", "Job exceeded its execution limit")
        if disk and time.monotonic() - self._last_disk_check > 2:
            self._last_disk_check = time.monotonic()
            size = sum(p.lstat().st_size for p in self.root.rglob("*") if p.is_file())
            if size > self.max_bytes:
                raise BackItUpError("disk_limit", "Job exceeded its temporary storage limit")


def safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or "\\" in value or "\x00" in value or path.is_absolute() or any(
        part in ("..", ".", "") for part in value.split("/")
    ) or ":" in path.parts[0]:
        raise BackItUpError("unsafe_path", "Snapshot contains an unsafe object path")
    return path


def sha256_file(path: Path, budget: Budget | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if budget:
                budget.check()
            digest.update(chunk)
    return digest.hexdigest()


class Git:
    def __init__(self, budget: Budget, token: Callable[[], str] | None = None, *, allow_local: bool = False):
        self.budget = budget
        self.token = token
        self.allow_local = allow_local
        self.auth_dir = budget.root / ".authentication"
        self.auth_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.askpass = self.auth_dir / "askpass"
        self.askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s" "x-access-token";; *) cat "$BACKITUP_GIT_TOKEN_FILE";; esac\n')
        self.askpass.chmod(0o700)

    def run(self, args: list[str], *, repo: Path | None = None, input_bytes: bytes | None = None) -> bytes:
        self.budget.check()
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(self.askpass.resolve()),
                    "GIT_LFS_SKIP_SMUDGE": "1", "GIT_SSH_COMMAND": "false"})
        token_file = self.auth_dir / "token"
        def update_token():
            if self.token:
                temporary = self.auth_dir / "token.new"
                temporary.write_text(self.token())
                temporary.chmod(0o600)
                temporary.replace(token_file)
        update_token()
        env["BACKITUP_GIT_TOKEN_FILE"] = str(token_file.resolve())
        token_checked = time.monotonic()
        command = ["git", "-c", f"core.hooksPath={self.auth_dir / 'no-hooks'}", "-c", "credential.helper=",
                   "-c", "protocol.ext.allow=never", "-c", "protocol.ssh.allow=never",
                   "-c", f"protocol.file.allow={'always' if self.allow_local else 'never'}",
                   "-c", "pack.threads=1", "-c", "pack.windowMemory=64m", "-c", "http.followRedirects=false"]
        if repo:
            command += ["--git-dir", str(repo.resolve())]
        # Subprocess output goes to bounded-disk files, never to logs or an archive.
        with (self.auth_dir / "stdout").open("w+b") as stdout, (self.auth_dir / "stderr").open("w+b") as stderr:
            proc = subprocess.Popen(command + args, env=env, stdin=subprocess.PIPE if input_bytes else subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                if input_bytes and proc.stdin:
                    proc.stdin.write(input_bytes)
                    proc.stdin.close()
                while proc.poll() is None:
                    self.budget.check()
                    if time.monotonic() - token_checked > 30:
                        update_token()
                        token_checked = time.monotonic()
                    time.sleep(0.1)
                self.budget.check()
                if proc.returncode:
                    # Git errors can contain tokens, headers, or hostile remote text.
                    raise BackItUpError("git_failed", f"Git {args[0]} failed; check repository access, permissions, and service limits")
                stdout.seek(0)
                result = stdout.read(64 * 1024 * 1024 + 1)
                if len(result) > 64 * 1024 * 1024:
                    raise BackItUpError("git_output_limit", "Git inventory exceeded the supported output limit")
                return result
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()

    def refs(self, repo: Path) -> dict[str, str]:
        output = self.run(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"], repo=repo)
        return dict(line.split(" ", 1) for line in output.decode().splitlines())

    def capture(self, source: str, target: Path) -> dict:
        if not self.allow_local and not source.startswith("https://github.com/"):
            raise BackItUpError("unsafe_source", "Only GitHub HTTPS repositories are supported")
        self.run(["clone", "--bare", "--no-local", "--", source, str(target.resolve())])
        self.run(["fetch", "--force", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], repo=target)
        self.run(["fsck", "--full", "--strict"], repo=target)
        refs = self.refs(target)
        object_format = self.run(["rev-parse", "--show-object-format"], repo=target).decode().strip()
        default = self.run(["symbolic-ref", "HEAD"], repo=target).decode().strip()
        # Only ordinary references belong in the recoverable snapshot.
        all_refs = self.run(["for-each-ref", "--format=%(refname)"], repo=target).decode().splitlines()
        for ref in all_refs:
            if ref not in refs:
                self.run(["update-ref", "-d", ref], repo=target)
        objects = self.run(["rev-list", "--objects", "--all"], repo=target).decode()
        workflows = any(" .github/workflows/" in line for line in objects.splitlines())
        return {"refs": refs, "default_branch": default.removeprefix("refs/heads/"), "object_format": object_format, "has_workflows": workflows}

    def remove_remote(self, repo: Path) -> None:
        remotes = self.run(["remote"], repo=repo).decode().splitlines()
        for name in remotes:
            self.run(["remote", "remove", name], repo=repo)
        # Rebuild config rather than carry authentication, includes, filters, or hooks.
        object_format = self.run(["rev-parse", "--show-object-format"], repo=repo).decode().strip()
        config = "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
        if object_format != "sha1":
            config = f"[core]\n\trepositoryformatversion = 1\n\tbare = true\n[extensions]\n\tobjectFormat = {object_format}\n"
        (repo / "config").write_text(config)
        shutil.rmtree(repo / "hooks", ignore_errors=True)

    def capture_lfs(self, repo: Path, source: str) -> list[dict]:
        self.run(["lfs", "version"])
        self.run(["-c", f"lfs.url={source}/info/lfs", "lfs", "fetch", "--all", "origin"], repo=repo)
        inventory = []
        # Enumerate pointers across captured history; historical deleted files matter.
        output = self.run(["lfs", "ls-files", "--all", "--long"], repo=repo).decode()
        for oid in sorted({line.split()[0] for line in output.splitlines() if line.strip()}):
            if len(oid) != 64 or any(c not in "0123456789abcdef" for c in oid):
                raise BackItUpError("lfs_inventory", "Invalid LFS object identifier")
            path = repo / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
            if not path.is_file() or sha256_file(path, self.budget) != oid:
                raise BackItUpError("lfs_missing", "A required LFS object is missing or corrupt")
            inventory.append({"oid": oid, "size": path.stat().st_size})
        return inventory

    def remote_refs(self, target: str) -> dict[str, str]:
        output = self.run(["ls-remote", "--refs", "--heads", "--tags", "--", target])
        return {ref: oid for oid, ref in (line.split("\t") for line in output.decode().splitlines())}

    def push(self, repo: Path, target: str, expected: dict[str, str], *, checkpoint: dict[str, str] | None = None) -> dict:
        current = self.remote_refs(target)
        if current != (checkpoint or {}):
            raise BackItUpError("destination_changed", "Destination references changed; recovery stopped to protect existing work")
        if expected:
            # Atomic push avoids an ambiguous half-applied ref set on supported remotes.
            self.run(["push", "--atomic", "--", target, *[f"{ref}:{ref}" for ref in expected]], repo=repo)
        actual = self.remote_refs(target)
        if actual != expected:
            raise BackItUpError("ref_mismatch", "Destination branches or tags do not match the snapshot")
        return {"expected": expected, "actual": actual, "verified": True}

    def push_lfs(self, repo: Path, target: str, inventory: list[dict]) -> None:
        if not inventory:
            return
        self.run(["remote", "add", "recovery", target], repo=repo)
        try:
            self.run(["-c", f"lfs.url={target}/info/lfs", "lfs", "push", "--all", "recovery"], repo=repo)
            # Fetch into an empty cache so verification reads destination data.
            verified = self.budget.root / "lfs-verification"
            self.run(["-c", f"lfs.url={target}/info/lfs", "-c", f"lfs.storage={verified.resolve()}", "lfs", "fetch", "--all", "recovery"], repo=repo)
            for item in inventory:
                oid = item["oid"]
                path = verified / "objects" / oid[:2] / oid[2:4] / oid
                if not path.is_file() or path.stat().st_size != item["size"] or sha256_file(path, self.budget) != oid:
                    raise BackItUpError("lfs_mismatch", "Destination LFS verification failed")
        finally:
            self.run(["remote", "remove", "recovery"], repo=repo)


def package_archive(root: Path, destination: Path, budget: Budget) -> dict:
    with destination.open("wb") as raw, zstandard.ZstdCompressor(level=3).stream_writer(raw) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as archive:
            for path in sorted(root.rglob("*")):
                budget.check()
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise BackItUpError("unsafe_file", "Backup contains an unsupported filesystem entry")
                relative = path.relative_to(root).as_posix()
                safe_relative(relative)
                info = archive.gettarinfo(str(path), arcname=relative)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o700 if path.is_dir() else 0o600
                if path.is_file():
                    with path.open("rb") as data:
                        archive.addfile(info, data)
                else:
                    archive.addfile(info)
    return {"size": destination.stat().st_size, "sha256": sha256_file(destination, budget)}


def extract_archive(source: Path, destination: Path, budget: Budget, *, max_entries: int = 1_000_000) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    total = 0
    seen: set[str] = set()
    try:
        with source.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as decoded:
            with tarfile.open(fileobj=decoded, mode="r|") as archive:
                for index, member in enumerate(archive):
                    budget.check()
                    if index >= max_entries or member.name in seen:
                        raise BackItUpError("unsafe_archive", "Archive has too many or duplicate entries")
                    seen.add(member.name)
                    relative = safe_relative(member.name)
                    if not (member.isfile() or member.isdir()) or member.size < 0:
                        raise BackItUpError("unsafe_archive", "Archive links and special files are forbidden")
                    total += member.size
                    if total > budget.max_bytes:
                        raise BackItUpError("disk_limit", "Expanded archive exceeds the recovery limit")
                    target = destination.joinpath(*relative.parts)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if member.isdir():
                        target.mkdir(mode=0o700, exist_ok=True)
                    else:
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise BackItUpError("unsafe_archive", "Archive entry has no contents")
                        with target.open("xb") as output:
                            while chunk := stream.read(1024 * 1024):
                                budget.check()
                                output.write(chunk)
    except (tarfile.TarError, zstandard.ZstdError, EOFError, OSError) as exc:
        raise BackItUpError("corrupt_archive", "Snapshot archive is truncated or invalid") from exc


def metadata_member(source: Path, category: str, *, max_bytes: int = 50_000_000) -> bytes:
    safe_relative(category)
    if "/" in category:
        raise BackItUpError("invalid_category", "Unknown metadata category")
    wanted = f"metadata/{category}.json"
    with source.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as decoded:
        with tarfile.open(fileobj=decoded, mode="r|") as archive:
            for member in archive:
                safe_relative(member.name)
                if member.name == wanted:
                    if not member.isfile() or member.size > max_bytes:
                        raise BackItUpError("metadata_limit", "Metadata archive is too large to download in one response")
                    stream = archive.extractfile(member)
                    if stream:
                        return stream.read(max_bytes + 1)
    raise BackItUpError("missing_metadata", "This metadata category was not captured", status=404)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
