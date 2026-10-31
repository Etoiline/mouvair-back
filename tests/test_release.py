"""Protège la cohérence du verrou pendant une release et en cas d’échec de préparation.

Les commandes externes sont simulées : ces scénarios ne créent aucun commit et
n’exigent ni accès réseau ni installation de uv pour lancer les tests unitaires.
"""

import subprocess
from pathlib import Path

import pytest
import release


@pytest.fixture
def release_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[Path, str]:
    """Prépare quatre fichiers isolés, avec des fins de ligne à conserver en cas d’échec."""
    lockfile = tmp_path / "uv.lock"
    lockfile.write_bytes(b"original lock\r\n")
    monkeypatch.setattr(release, "LOCKFILE_PATH", lockfile)
    contents = {tmp_path / name: f"next {name}\n" for name in (
        "pyproject.toml", "__init__.py", "CHANGELOG.md",
    )}
    for path in contents:
        path.write_bytes(b"original content\r\n")
    return contents


def test_release_refreshes_lock_before_accepting_prepared_files(
    release_files: dict[Path, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le verrou doit être recalculé hors ligne une fois la nouvelle version écrite."""
    calls = []

    def update_lock(*arguments: str) -> None:
        assert all(path.read_text() == content for path, content in release_files.items())
        calls.append(arguments)
        release.LOCKFILE_PATH.write_text("next lock\n", encoding="utf-8")

    monkeypatch.setattr(release, "run_uv", update_lock)
    release.write_release_files(release_files)

    assert calls == [("lock", "--offline")]
    assert release.LOCKFILE_PATH.read_text() == "next lock\n"


def test_failed_lock_refresh_restores_all_original_bytes(
    release_files: dict[Path, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une erreur de uv restaure également le verrou et les fins de ligne originales."""
    originals = {path: path.read_bytes() for path in (*release_files, release.LOCKFILE_PATH)}

    def fail_update(*arguments: str) -> None:
        release.LOCKFILE_PATH.write_text("partial lock\n", encoding="utf-8")
        raise release.ReleaseError("simulated uv failure")

    monkeypatch.setattr(release, "run_uv", fail_update)
    with pytest.raises(release.ReleaseError, match="restaurés"):
        release.write_release_files(release_files)

    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize("missing_uv", [False, True])
def test_uv_failures_are_reported_as_release_errors(
    monkeypatch: pytest.MonkeyPatch, missing_uv: bool,
) -> None:
    """L’absence de l’outil et un verrou refusé empêchent une release avec une erreur lisible."""
    def fail_uv(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["uv", "lock", "--check", "--offline"]
        assert kwargs["cwd"] == release.PROJECT_ROOT
        if missing_uv:
            raise FileNotFoundError
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="stale lock")

    monkeypatch.setattr(release.subprocess, "run", fail_uv)
    with pytest.raises(release.ReleaseError, match="introuvable" if missing_uv else "stale lock"):
        release.run_uv("lock", "--check", "--offline")


@pytest.mark.parametrize("dry_run", [True, False])
def test_release_commits_the_updated_lock_and_dry_run_leaves_files_unchanged(
    release_files: dict[Path, str], monkeypatch: pytest.MonkeyPatch, dry_run: bool,
) -> None:
    """Une release inclut le verrou ; sa prévisualisation reste sans écriture ni commit."""
    root = release.LOCKFILE_PATH.parent
    paths = {path.name: path for path in release_files}
    paths["pyproject.toml"].write_text('version = "0.4.0"\n', encoding="utf-8")
    paths["__init__.py"].write_text('__version__ = "0.4.0"\n', encoding="utf-8")
    paths["CHANGELOG.md"].write_text(
        "# Changelog\n\n- Pending change.\n\n## 0.4.0 - 2026-10-30\n\n- Previous change.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "PROJECT_ROOT", root)
    monkeypatch.setattr(release, "PYPROJECT_PATH", paths["pyproject.toml"])
    monkeypatch.setattr(release, "PACKAGE_INIT_PATH", paths["__init__.py"])
    monkeypatch.setattr(release, "CHANGELOG_PATH", paths["CHANGELOG.md"])
    monkeypatch.setattr(release, "ensure_clean_repository", lambda: None)
    monkeypatch.setattr(release, "ask_bump", lambda _: "patch")
    arguments = ["release.py", "--dry-run"] if dry_run else ["release.py"]
    monkeypatch.setattr(release.sys, "argv", arguments)
    originals = {path: path.read_bytes() for path in (*release_files, release.LOCKFILE_PATH)}
    uv_calls = []
    git_calls = []

    def update_lock(*arguments: str) -> None:
        uv_calls.append(arguments)
        if arguments == ("lock", "--offline"):
            assert 'version = "0.4.1"' in paths["pyproject.toml"].read_text()
            release.LOCKFILE_PATH.write_text("next lock\n", encoding="utf-8")

    def record_git(*arguments: str, **kwargs: object) -> str:
        git_calls.append(arguments)
        return ""

    monkeypatch.setattr(release, "run_uv", update_lock)
    monkeypatch.setattr(release, "run_git", record_git)
    release.main()

    assert uv_calls[0] == ("lock", "--check", "--offline")
    if dry_run:
        assert len(uv_calls) == 1
        assert not git_calls
        assert {path: path.read_bytes() for path in originals} == originals
    else:
        assert uv_calls[1:] == [("lock", "--offline")]
        assert git_calls[0] == (
            "add", "--", "pyproject.toml", "__init__.py", "CHANGELOG.md", "uv.lock",
        )
        assert git_calls[1] == ("commit", "-m", "🔖 Release 0.4.1")
        assert '__version__ = "0.4.1"' in paths["__init__.py"].read_text()
        assert release.LOCKFILE_PATH.read_text() == "next lock\n"
