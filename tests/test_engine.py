import subprocess
import tarfile
from pathlib import Path

import pytest
import zstandard
from backitup.engine import Budget, Git, extract_archive, package_archive
from backitup.errors import BackItUpError


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_capture_package_extract_and_restore_preserves_refs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "BackItUp test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "README.md").write_text("first version\n")
    (source / ".github" / "workflows").mkdir(parents=True)
    (source / ".github" / "workflows" / "check.yml").write_text("name: check\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "first")
    git(source, "tag", "v1.0.0")
    git(source, "checkout", "-b", "release")
    (source / "README.md").write_text("release branch\n")
    git(source, "commit", "-am", "release")
    git(source, "checkout", "main")

    scratch = tmp_path / "scratch"
    budget = Budget(scratch, max_bytes=100_000_000)
    client = Git(budget, allow_local=True)
    captured = tmp_path / "captured.git"
    inventory = client.capture(source.as_uri(), captured)
    client.remove_remote(captured)

    assert inventory["default_branch"] == "main"
    assert inventory["has_workflows"] is True
    assert set(inventory["refs"]) == {"refs/heads/main", "refs/heads/release", "refs/tags/v1.0.0"}
    assert "remote" not in (captured / "config").read_text()

    archive = tmp_path / "snapshot.tar.zst"
    package_archive(captured, archive, budget)
    extracted = tmp_path / "extracted"
    extract_archive(archive, extracted, budget)

    destination = tmp_path / "destination.git"
    subprocess.run(["git", "init", "--bare", str(destination)], check=True, capture_output=True)
    result = client.push(extracted, destination.as_uri(), inventory["refs"])
    assert result["verified"] is True


def test_safe_extraction_rejects_parent_path(tmp_path: Path) -> None:
    archive_path = tmp_path / "hostile.tar.zst"
    with archive_path.open("wb") as raw, zstandard.ZstdCompressor().stream_writer(raw) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as archive:
            member = tarfile.TarInfo("../outside.txt")
            member.size = 0
            archive.addfile(member)

    with pytest.raises(BackItUpError, match="unsafe object path"):
        extract_archive(archive_path, tmp_path / "restore", Budget(tmp_path, max_bytes=10_000_000))
