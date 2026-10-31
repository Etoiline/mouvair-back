"""Crée une release locale du backend à partir des changements du changelog.

Le script vérifie d’abord un dépôt propre, les versions synchronisées et la
présence de changements non publiés. Il demande un niveau SemVer, prépare les
fichiers et actualise uv.lock hors ligne avant le commit. --dry-run conserve ces validations mais
s’arrête avant toute écriture. Le script ne pousse pas et ne crée pas de tag Git.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Les chemins dépendent du script et restent corrects quel que soit le dossier d’appel.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
PACKAGE_INIT_PATH = PROJECT_ROOT / "src" / "mouvair_api" / "__init__.py"
CHANGELOG_PATH = PROJECT_ROOT / "CHANGELOG.md"
LOCKFILE_PATH = PROJECT_ROOT / "uv.lock"
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
        """Calcule la prochaine version et remet à zéro les niveaux inférieurs.

        Une majeure remet mineure et corrective à zéro ; une mineure remet
        seulement la corrective à zéro. Aucune mutation de la version courante.
        """

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
    """Exécute Git à la racine et transforme ses échecs en erreurs lisibles.

    Les arguments sont transmis comme liste, sans interprétation par un shell.
    La capture sert aux vérifications ; les commandes de commit gardent leur
    sortie normale pour rendre l’opération visible dans le terminal.
    """

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
    """Vérifie la racine et l’absence de changements avant de préparer la release.

    Ce contrôle empêche d’embarquer des modifications de travail ou d’exécuter
    l’opération dans le dépôt parent d’un projet imbriqué.
    """

    repository_root = Path(run_git("rev-parse", "--show-toplevel", capture_output=True)).resolve()
    if repository_root != PROJECT_ROOT.resolve():
        raise ReleaseError(f"La racine Git attendue est {PROJECT_ROOT}, pas {repository_root}.")

    status = run_git("status", "--porcelain", capture_output=True)
    if status:
        raise ReleaseError(
            "Le dépôt doit être propre avant une release. Committez ou annulez ces changements :\n"
            f"{status}"
        )


def run_uv(*arguments: str) -> None:
    """Vérifie ou actualise le verrou avec la version de uv exigée par le projet."""

    try:
        result = subprocess.run(
            ["uv", *arguments],
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise ReleaseError(
            "uv est introuvable. Installez la version indiquée dans le README."
        ) from error

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleaseError(f"La commande uv a échoué : uv {' '.join(arguments)}\n{detail}")


def write_release_files(contents: dict[Path, str]) -> None:
    """Synchronise les fichiers et le verrou, avec restauration si la préparation échoue.

    uv conserve les versions déjà résolues et modifie les métadonnées du package
    local sans accès réseau. Aucun fichier n’est stagé avant cette étape réussie.
    """

    originals = {path: path.read_bytes() for path in (*contents, LOCKFILE_PATH)}
    try:
        for path, content in contents.items():
            write_text(path, content)
        run_uv("lock", "--offline")
    except (ReleaseError, OSError) as error:
        for path, original in originals.items():
            path.write_bytes(original)
        raise ReleaseError(
            "La préparation de la release a échoué ; les fichiers ont été restaurés.\n"
            f"{error}"
        ) from error


def read_project_version(pyproject: str, package_init: str) -> Version:
    """Exige une version unique et identique dans le package et dans pyproject.toml.

    Les expressions régulières modifient ensuite uniquement ces lignes, sans
    réécrire les commentaires ni le reste des fichiers de configuration.
    """

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
    """Valide les changements non publiés et leur position avant la dernière version.

    Le bloc doit contenir une puce sans titre intermédiaire. La correspondance
    renvoyée indique où insérer le futur titre pour englober ces changements.
    """

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
    """Affiche les trois versions possibles et demande un choix jusqu’à sa validation.

    Une saisie vide choisit patch ; les chiffres sont des raccourcis. Une fin de
    flux ou une interruption annule proprement avant toute écriture de fichier.
    """

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
    """Écrit en UTF-8 avec des fins LF, indépendamment de la plateforme de release."""

    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(content.replace("\r\n", "\n"))


def build_parser() -> argparse.ArgumentParser:
    """Déclare --dry-run ; le choix du niveau de version reste interactif dans ce mode."""

    parser = argparse.ArgumentParser(description="Créer une release locale du backend Mouv’Air.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Afficher le résultat sans modifier les fichiers ni créer de commit.",
    )
    return parser


def main() -> None:
    """Valide, prépare en mémoire, puis écrit et committe la prochaine release.

    Les contenus sont calculés avant la branche dry-run. Le commit ne
    stage que les fichiers de version, le changelog et le verrou ; un dernier contrôle
    signale tout changement restant après l’opération.
    """

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    arguments = build_parser().parse_args()
    ensure_clean_repository()
    # Un verrou absent ou périmé doit être corrigé avant de commencer une release.
    run_uv("lock", "--check", "--offline")

    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    package_init = PACKAGE_INIT_PATH.read_text(encoding="utf-8")
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    current_version = read_project_version(pyproject, package_init)
    changelog_start = validate_changelog(changelog, current_version)
    next_version = current_version.bump(ask_bump(current_version))
    release_date = date.today().isoformat()
    release_heading = f"## {next_version} - {release_date}\n\n"

    # La préparation reste sans effet de bord, ce qui permet la prévisualisation complète.
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

    # La première écriture intervient seulement après tous les contrôles et le choix SemVer.
    write_release_files({
        PYPROJECT_PATH: next_pyproject,
        PACKAGE_INIT_PATH: next_package_init,
        CHANGELOG_PATH: next_changelog,
    })
    run_git(
        "add",
        "--",
        PYPROJECT_PATH.name,
        PACKAGE_INIT_PATH.relative_to(PROJECT_ROOT).as_posix(),
        CHANGELOG_PATH.name,
        LOCKFILE_PATH.name,
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
