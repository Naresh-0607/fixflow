import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..core.config import Settings
from ..core.exceptions import (
    PatchApplicationError,
    RepositoryStateError,
    UnsafeModificationError,
)
from ..models.responses import RepairChange


IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
FORBIDDEN_PATCH_MARKERS = (
    "GIT binary patch",
    "Binary files ",
    "new file mode ",
    "deleted file mode ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "old mode ",
    "new mode ",
)


@dataclass(frozen=True)
class PatchTransaction:
    originals: dict[str, bytes]
    files: list[str]


class RepositoryTools:
    """Small, path-confined operations. No method accepts a shell command."""

    def __init__(self, repository_path: Path, settings: Settings) -> None:
        self.repository_path = repository_path.resolve()
        self.settings = settings
        if not self.repository_path.is_dir():
            raise RepositoryStateError(
                "The job repository directory does not exist."
            )

    def list_files(self) -> list[str]:
        files: list[str] = []
        for root, directories, filenames in os.walk(
            self.repository_path, followlinks=False
        ):
            root_path = Path(root)
            directories[:] = sorted(
                name
                for name in directories
                if name not in IGNORED_DIRECTORIES
                and not (root_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = root_path / filename
                if path.is_symlink():
                    continue
                files.append(path.relative_to(self.repository_path).as_posix())
                if len(files) >= self.settings.max_tree_entries:
                    return files
        return files

    def read_file(self, relative_path: str) -> str:
        path = self._resolve_existing_file(relative_path)
        self._reject_binary(path)
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise UnsafeModificationError(
                f"File is not valid UTF-8 text: {relative_path}"
            ) from exc

    def search_code(self, query: str) -> list[dict[str, object]]:
        if not query:
            return []
        results: list[dict[str, object]] = []
        for relative_path in self.list_files():
            path = self._resolve_existing_file(relative_path)
            if self._is_binary(path):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    results.append(
                        {
                            "file": relative_path,
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(results) >= self.settings.max_search_results:
                        return results
        return results

    def apply_patch(self, changes: list[RepairChange]) -> PatchTransaction:
        if not changes:
            raise PatchApplicationError("Gemini returned no file changes.")
        if len(changes) > self.settings.max_files_per_iteration:
            raise UnsafeModificationError(
                "The repair exceeds the maximum files allowed per iteration.",
                details={
                    "requested": len(changes),
                    "maximum": self.settings.max_files_per_iteration,
                },
            )

        total_size = sum(len(change.patch) for change in changes)
        if total_size > self.settings.max_patch_characters:
            raise UnsafeModificationError(
                "The repair patch exceeds the configured size limit.",
                details={
                    "requested_characters": total_size,
                    "maximum_characters": self.settings.max_patch_characters,
                },
            )

        prepared: list[tuple[str, str]] = []
        originals: dict[str, bytes] = {}
        seen: set[str] = set()
        for change in changes:
            path = self._resolve_existing_file(change.file)
            relative = path.relative_to(self.repository_path).as_posix()
            if relative in seen:
                raise PatchApplicationError(
                    f"A repair may patch a file only once: {relative}"
                )
            seen.add(relative)
            self._reject_binary(path)
            normalized_patch = self._validate_patch(relative, change.patch)
            originals[relative] = path.read_bytes()
            prepared.append((relative, normalized_patch))

        applied: list[str] = []
        try:
            for relative, patch in prepared:
                self._git_apply(patch, check=True)
                self._git_apply(patch, check=False)
                applied.append(relative)
        except Exception:
            for relative, content in originals.items():
                path = self._resolve_existing_file(relative)
                path.write_bytes(content)
            raise
        return PatchTransaction(originals=originals, files=applied)

    def restore_file(self, relative_path: str, content: bytes) -> None:
        path = self._resolve_existing_file(relative_path)
        path.write_bytes(content)

    def restore_transaction(self, transaction: PatchTransaction) -> None:
        for relative_path, content in transaction.originals.items():
            self.restore_file(relative_path, content)

    def initial_commit(self) -> str:
        return self._run_git(["rev-parse", "HEAD"]).strip()

    def git_status(self) -> str:
        return self._run_git(["status", "--short"])

    def get_git_diff(self) -> str:
        return self._run_git(["diff", "--no-ext-diff", "--no-color"])

    def changed_files(self) -> list[str]:
        output = self._run_git(["diff", "--name-only", "--no-ext-diff"])
        return [line for line in output.splitlines() if line]

    def diff_stats(self) -> dict[str, object]:
        output = self._run_git(["diff", "--numstat", "--no-ext-diff"])
        additions = 0
        deletions = 0
        files: list[str] = []
        for line in output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, filename = parts
            files.append(filename)
            if added.isdigit():
                additions += int(added)
            if deleted.isdigit():
                deletions += int(deleted)
        return {"files": files, "additions": additions, "deletions": deletions}

    def _resolve_existing_file(self, raw_path: str) -> Path:
        if not raw_path or "\x00" in raw_path:
            raise UnsafeModificationError("A repair file path is empty or invalid.")
        normalized = raw_path.replace("\\", "/")
        posix_path = PurePosixPath(normalized)
        windows_path = PureWindowsPath(raw_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or ".." in posix_path.parts
            or ".git" in posix_path.parts
        ):
            raise UnsafeModificationError(
                f"Repair path is outside the repository boundary: {raw_path}"
            )
        candidate = self.repository_path.joinpath(*posix_path.parts)
        if candidate.is_symlink():
            raise UnsafeModificationError(
                f"Symbolic links cannot be modified: {raw_path}"
            )
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.repository_path)
        except (OSError, ValueError) as exc:
            raise UnsafeModificationError(
                f"Repair path does not resolve to a repository file: {raw_path}"
            ) from exc
        if not resolved.is_file():
            raise UnsafeModificationError(
                f"Repair target must be an existing regular file: {raw_path}"
            )
        return resolved

    def _validate_patch(self, relative_path: str, patch: str) -> str:
        if not patch.strip() or "\x00" in patch:
            raise PatchApplicationError("The generated patch is empty or invalid.")
        if any(marker in patch for marker in FORBIDDEN_PATCH_MARKERS):
            raise UnsafeModificationError(
                "Binary, file creation, deletion, copy, and rename patches are not allowed."
            )

        normalized = patch.replace("\r\n", "\n")
        if normalized.lstrip().startswith("@@"):
            normalized = (
                f"--- a/{relative_path}\n+++ b/{relative_path}\n"
                + normalized.lstrip()
            )

        old_paths = self._patch_header_paths(normalized, "--- ")
        new_paths = self._patch_header_paths(normalized, "+++ ")
        if len(old_paths) != 1 or len(new_paths) != 1:
            raise PatchApplicationError(
                "Each repair change must contain one unified diff for its declared file."
            )
        expected = relative_path.replace("\\", "/")
        if old_paths[0] != expected or new_paths[0] != expected:
            raise UnsafeModificationError(
                "The unified diff path does not match the declared repair file.",
                details={"declared_file": expected},
            )
        diff_headers = [
            line for line in normalized.splitlines() if line.startswith("diff --git ")
        ]
        expected_header = f"diff --git a/{expected} b/{expected}"
        if diff_headers and diff_headers != [expected_header]:
            raise UnsafeModificationError(
                "The Git diff metadata does not match the declared repair file."
            )
        if not re.search(r"(?m)^@@\s", normalized):
            raise PatchApplicationError("The unified diff contains no patch hunks.")
        return normalized if normalized.endswith("\n") else normalized + "\n"

    @staticmethod
    def _patch_header_paths(patch: str, prefix: str) -> list[str]:
        paths: list[str] = []
        for line in patch.splitlines():
            if not line.startswith(prefix):
                continue
            raw = line[len(prefix) :].split("\t", 1)[0].strip()
            if raw == "/dev/null" or raw.startswith('"'):
                paths.append(raw)
                continue
            if raw.startswith("a/") or raw.startswith("b/"):
                raw = raw[2:]
            paths.append(raw.replace("\\", "/"))
        return paths

    def _git_apply(self, patch: str, *, check: bool) -> None:
        command = ["git", "apply", "--recount", "--whitespace=nowarn"]
        if check:
            command.append("--check")
        command.append("-")
        result = subprocess.run(
            command,
            cwd=self.repository_path,
            input=patch.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout).decode(
                "utf-8", errors="replace"
            )
            raise PatchApplicationError(
                "The generated unified diff could not be applied cleanly.",
                details={"git_error": error[-3000:]},
            )

    def _run_git(self, arguments: list[str]) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repository_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise RepositoryStateError(
                "Unable to inspect the cloned repository's Git state.",
                details={"git_error": (result.stderr or result.stdout)[-3000:]},
            )
        return result.stdout

    @staticmethod
    def _is_binary(path: Path) -> bool:
        try:
            return b"\x00" in path.read_bytes()[:8192]
        except OSError:
            return True

    def _reject_binary(self, path: Path) -> None:
        if self._is_binary(path):
            raise UnsafeModificationError(
                f"Binary files cannot be read or modified: "
                f"{path.relative_to(self.repository_path).as_posix()}"
            )
