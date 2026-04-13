
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from git import Repo, GitCommandError

from app.core.models import Evidence
from app.core import config
from app.llm import llm_json

logger = logging.getLogger(__name__)


CODE_ANALYSIS_SYSTEM = """You analyze source code files to identify what capabilities they IMPLEMENT.

Rules:
- Only list capabilities with clear code evidence. No speculation.
- Distinguish implemented (code runs) vs imported-but-unused.
- Be technical and specific.

Return a JSON object:
{
  "implements": ["capability 1", "capability 2"],
  "tech_signals": ["framework/lib names detected"],
  "entry_points": ["route paths, exported functions, CLI commands"],
  "notes": "one-line technical observation"
}
Return empty lists if nothing notable.
"""


def _is_code_file(path: Path) -> bool:
    if path.name in config.IMPORTANT_FILES:
        return True
    if path.suffix.lower() in config.CODE_EXTENSIONS:
        return True
    return False


def _should_skip(path: Path) -> bool:
    """Skip generated/vendored/hidden directories."""
    skip_dirs = {"node_modules", ".git", "venv", ".venv", "dist", "build",
                 "__pycache__", ".next", "target", "vendor"}
    return any(part in skip_dirs or part.startswith(".") for part in path.parts)


def _clone_repo(repo_url: str) -> Path:
    """Clone a public git repo to a temp dir. Returns the path."""
    tmp = Path(tempfile.mkdtemp(prefix="evidence_repo_"))
    try:
        Repo.clone_from(repo_url, tmp, depth=1)
        return tmp
    except GitCommandError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"Failed to clone repo: {e}")


def _collect_files(root: Path) -> list[Path]:
    """Pick the most interesting files from the repo."""
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or _should_skip(path.relative_to(root)):
            continue
        if not _is_code_file(path):
            continue
        try:
            size_kb = path.stat().st_size / 1024
            if size_kb > config.MAX_FILE_SIZE_KB:
                continue
        except OSError:
            continue
        files.append(path)

    # Prioritize: important files first, then by shallower depth
    files.sort(key=lambda p: (
        0 if p.name in config.IMPORTANT_FILES else 1,
        len(p.parts),
        str(p),
    ))
    return files[: config.MAX_CODE_FILES]


def _analyze_file(file_path: Path, rel_path: str) -> list[Evidence]:
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    if len(content.strip()) < 30:
        return []

    # Truncate very long files for LLM context
    snippet = content[:6000]
    user_msg = f"File: {rel_path}\n\n```\n{snippet}\n```\n\nAnalyze."
    result = llm_json(CODE_ANALYSIS_SYSTEM, user_msg)

    if not isinstance(result, dict):
        return []

    evidences = []
    for capability in result.get("implements", []):
        if not capability or not isinstance(capability, str):
            continue
        evidences.append(Evidence(
            source_type="code",
            source_id=rel_path,
            claim_or_fact=capability,
            evidence_text=f"Implemented in {rel_path}: {capability}",
            confidence=0.85,
            metadata={
                "file_path": rel_path,
                "tech_signals": result.get("tech_signals", []),
                "entry_points": result.get("entry_points", []),
                "notes": result.get("notes", ""),
            },
        ))
    return evidences


def extract_from_repo(repo_url_or_path: str) -> list[Evidence]:
    """
    Main entry: accepts either a git URL (gets cloned) or a local path.
    """
    is_url = repo_url_or_path.startswith(("http://", "https://", "git@"))
    tmp_path: Optional[Path] = None
    try:
        if is_url:
            logger.info("Cloning %s", repo_url_or_path)
            root = _clone_repo(repo_url_or_path)
            tmp_path = root
        else:
            root = Path(repo_url_or_path)
            if not root.exists():
                logger.warning("Repo path not found: %s", root)
                return []

        files = _collect_files(root)
        logger.info("Analyzing %d files", len(files))

        all_evidence = []
        for f in files:
            rel = str(f.relative_to(root))
            try:
                all_evidence.extend(_analyze_file(f, rel))
            except Exception as e:
                logger.warning("Failed on %s: %s", rel, e)
        return all_evidence
    finally:
        if tmp_path:
            shutil.rmtree(tmp_path, ignore_errors=True)
