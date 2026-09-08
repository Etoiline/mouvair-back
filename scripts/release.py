"""Crée une release locale du backend à partir des changements du changelog."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
PACKAGE_INIT_PATH = PROJECT_ROOT / "src" / "mouvair_api" / "__init__.py"
CHANGELOG_PATH = PROJECT_ROOT / "CHANGELOG.md"
PROJECT_VERSION_PATTERN = re.compile(r'(?m)^version = "(?P<version>[^"]+)"\s*$')
PACKAGE_VERSION_PATTERN = re.compile(r'(?m)^__version__ = "(?P<version>[^"]+)"\s*$')
RELEASE_HEADING_PATTERN = re.compile(
    r"(?m)^## (?P<version>\d+\.\d+\.\d+) - \d{4}-\d{2}-\d{2}\s*$"
)
CHANGELOG_START_PATTERN = re.compile(r"\A# Changelog(?:\r?\n){2}")


class ReleaseError(RuntimeError):
    """Erreur attendue empêchant de créer une release fiable."""


@dataclass(frozen=True)
class Version:
    """Version sémantique stricte du projet."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> Version:
        """Convertit une chaîne MAJOR.MINOR.PATCH en version structurée."""

        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
        if not match:
            raise ReleaseError(
                f'Version invalide "{value}". Format attendu : MAJOR.MINOR.PATCH.'
            )
        return cls(*(int(part) for part in match.groups()))

    def bump(self, part: str) -> Version:
        """Calcule la prochaine version pour le niveau SemVer choisi."""

        if part == "major":
            return Version(self.major + 1, 0, 0)
        if part == "minor":
            return Version(self.major, self.minor + 1, 0)
        if part == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ReleaseError(f'Niveau de version inconnu : "{part}".')

    def __str__(self) -> str:
        """Retourne la représentation SemVer de la version."""

        return f"{self.major}.{self.minor}.{self.patch}"


def run_git(*arguments: str, capture_output: bool = False) -> str:
    """Exécute Git à la racine et transforme ses échecs en erreurs lisibles."""

    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=capture_output,
        )
    except FileNotFoundError as error:
        raise ReleaseError("Git est introuvable sur cette machine.") from error

    if result.returncode != 0:
        detail = ""
        if capture_output:
            detail = (result.stderr or result.stdout).strip()
        suffix = f"\n{detail}" if detail else ""
        raise ReleaseError(f"La commande Git a échoué : git {' '.join(arguments)}{suffix}")
    return result.stdout.strip() if capture_output else ""


def ensure_clean_repository() -> None:
    """Vérifie que le script agit bien sur sa racine et sur un dépôt propre."""

    repository_root = Path(run_git("rev-parse", "--show-toplevel", capture_output=True)).resolve()
    if repository_root != PROJECT_ROOT.resolve():
        raise ReleaseError(f"La racine Git attendue est {PROJECT_ROOT}, pas {repository_root}.")

    status = run_git("status", "--porcelain", capture_output=True)
    if status:
        raise ReleaseError(
            "Le dépôt doit être propre avant une release. Committez ou annulez ces changements :\n"
            f"{status}"
        )


def read_project_version(pyproject: str, package_init: str) -> Version:
    """Lit la version et vérifie ses deux sources avant de la modifier."""

    matches = list(PROJECT_VERSION_PATTERN.finditer(pyproject))
    if len(matches) != 1:
        raise ReleaseError("pyproject.toml doit contenir une unique ligne de version du projet.")
    project_version = Version.parse(matches[0].group("version"))

    package_matches = list(PACKAGE_VERSION_PATTERN.finditer(package_init))
    if len(package_matches) != 1:
        raise ReleaseError("mouvair_api/__init__.py doit contenir une unique ligne __version__.")
    package_version = Version.parse(package_matches[0].group("version"))
    if package_version != project_version:
        raise ReleaseError(
            "Versions désynchronisées : "
            f"pyproject.toml={project_version}, mouvair_api.__version__={package_version}."
        )
    return project_version


def validate_changelog(changelog: str, current_version: Version) -> re.Match[str]:
    """Valide le bloc non publié et la cohérence de la dernière release."""

    start = CHANGELOG_START_PATTERN.match(changelog)
    if not start:
        raise ReleaseError('CHANGELOG.md doit commencer par "# Changelog" suivi d’une ligne vide.')

    latest_release = RELEASE_HEADING_PATTERN.search(changelog, start.end())
    if not latest_release:
        raise ReleaseError("CHANGELOG.md ne contient aucune section de version valide.")

    pending_changes = changelog[start.end() : latest_release.start()]
    if re.search(r"(?m)^#{1,6}\s", pending_changes):
        raise ReleaseError(
            "Les changements non publiés doivent rester sans titre entre # Changelog et la "
            "dernière version."
        )
    if not re.search(r"(?m)^-\s+\S", pending_changes):
        raise ReleaseError("Aucun changement non publié n’est présent en haut de CHANGELOG.md.")

    changelog_version = Version.parse(latest_release.group("version"))
    if changelog_version != current_version:
        raise ReleaseError(
            "Versions désynchronisées : "
            f"pyproject.toml={current_version}, CHANGELOG.md={changelog_version}."
        )
    return start


def ask_bump(current_version: Version) -> str:
    """Demande le niveau SemVer jusqu'à obtenir un choix reconnu."""

    choices = {"1": "patch", "2": "minor", "3": "major"}
    print(f"Version actuelle : {current_version}")
    print(f"1. patch  -> {current_version.bump('patch')}")
    print(f"2. minor  -> {current_version.bump('minor')}")
    print(f"3. major  -> {current_version.bump('major')}")

    while True:
        try:
            answer = input("Choisissez le niveau de version [patch] : ").strip().lower()
        except (EOFError, KeyboardInterrupt) as error:
            raise ReleaseError("Release annulée.") from error
        answer = answer or "patch"
        answer = choices.get(answer, answer)
        if answer in {"patch", "minor", "major"}:
            return answer
        print("Choix invalide. Saisissez patch, minor, major, 1, 2 ou 3.")


def write_text(path: Path, content: str) -> None:
    """Écrit un fichier UTF-8 avec des fins de ligne stables."""

    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(content.replace("\r\n", "\n"))


def build_parser() -> argparse.ArgumentParser:
    """Construit les options non interactives complémentaires."""

    parser = argparse.ArgumentParser(description="Créer une release locale du backend Mouv’Air.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Afficher le résultat sans modifier les fichiers ni créer de commit.",
    )
    return parser


def main() -> None:
    """Valide, versionne et committe la prochaine release du backend."""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    arguments = build_parser().parse_args()
    ensure_clean_repository()

    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    package_init = PACKAGE_INIT_PATH.read_text(encoding="utf-8")
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    current_version = read_project_version(pyproject, package_init)
    changelog_start = validate_changelog(changelog, current_version)
    next_version = current_version.bump(ask_bump(current_version))
    release_date = date.today().isoformat()
    release_heading = f"## {next_version} - {release_date}\n\n"

    next_pyproject = PROJECT_VERSION_PATTERN.sub(
        f'version = "{next_version}"', pyproject, count=1
    )
    next_package_init = PACKAGE_VERSION_PATTERN.sub(
        f'__version__ = "{next_version}"', package_init, count=1
    )
    next_changelog = (
        changelog[: changelog_start.end()]
        + release_heading
        + changelog[changelog_start.end() :]
    )

    if arguments.dry_run:
        print(f"Prévisualisation : {current_version} -> {next_version}")
        print(f"Entrée du changelog : {release_heading.strip()}")
        print(f"Commit : 🔖 Release {next_version}")
        return

    write_text(PYPROJECT_PATH, next_pyproject)
    write_text(PACKAGE_INIT_PATH, next_package_init)
    write_text(CHANGELOG_PATH, next_changelog)
    run_git(
        "add",
        "--",
        PYPROJECT_PATH.name,
        PACKAGE_INIT_PATH.relative_to(PROJECT_ROOT).as_posix(),
        CHANGELOG_PATH.name,
    )
    run_git("commit", "-m", f"🔖 Release {next_version}")

    remaining_changes = run_git("status", "--porcelain", capture_output=True)
    if remaining_changes:
        raise ReleaseError(
            f"Le commit a été créé, mais le dépôt n’est plus propre :\n{remaining_changes}"
        )
    print(f"Release {next_version} créée dans le commit courant.")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as error:
        print(f"Erreur : {error}", file=sys.stderr)
        raise SystemExit(1) from error
