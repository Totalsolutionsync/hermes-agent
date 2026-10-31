"""Skill bundle model: read from disk, hash like Kynver, validate untrusted input.

Hash rules mirror ``skill-bundle.hash.ts`` in Kynver (byte-for-byte, no line
ending normalisation)::

    fileHash    = sha256(content)
    contentHash = sha256("SKILL.md\\0" + sha256(skillMd) + "\\n"
                         + for each file sorted by path: path + "\\0" + fileHash + "\\n")
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SKILL_MD_MAX_BYTES = 24 * 1024
SKILL_FILE_MAX_BYTES = 256 * 1024
SKILL_BUNDLE_MAX_BYTES = 2 * 1024 * 1024
SKILL_BUNDLE_MAX_FILES = 400
SKILL_FILE_PATH_MAX_CHARS = 200
SIDECAR_NAME = ".kynver.json"
SERVER_COPY_NAME = ".kynver.server.json"
_SUPPORT_DIRS = ("references", "templates", "scripts", "assets")

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{30,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+=]{24,}"),
]


class BundleError(ValueError):
    """Raised for a bundle that must not be applied or pushed."""


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Bundle:
    skill_md: str
    files: dict[str, str] = field(default_factory=dict)  # posix path -> content

    @property
    def content_hash(self) -> str:
        return compute_content_hash(self.skill_md, self.files)


def compute_content_hash(skill_md: str, files: Optional[dict[str, str]] = None) -> str:
    lines = "".join(
        f"{path}\x00{sha256_hex(files[path])}\n" for path in sorted((files or {}), key=_code_unit_key)
    )
    return sha256_hex(f"SKILL.md\x00{sha256_hex(skill_md)}\n{lines}")


def _code_unit_key(path: str) -> bytes:
    # JS compares UTF-16 code units; for BMP text that equals code point order.
    return path.encode("utf-16-be")


def slug_for(name: str) -> str:
    """Hermes skill name -> Kynver slug (lowercase, ``_`` -> ``-``)."""
    return (name or "").strip().lower().replace("_", "-")


def find_secret(text: str) -> Optional[str]:
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return "contains a key, token or secret-looking value"
    return None


def check_rel_path(path: str) -> Optional[str]:
    """Reason a supporting-file path is unsafe, else None."""
    if not path or len(path) > SKILL_FILE_PATH_MAX_CHARS:
        return "path empty or too long"
    if "\x00" in path or "\\" in path:
        return "path contains NUL or backslash"
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith("/"):
        return "absolute path"
    if any(part in ("", ".", "..") for part in pure.parts):
        return "path traversal"
    if pure.parts[0].startswith("."):
        return "hidden path"
    return None


def read_bundle(skill_dir: Path) -> Bundle:
    """Read SKILL.md + text supporting files. Symlinks and binaries are skipped."""
    md_path = skill_dir / "SKILL.md"
    if md_path.is_symlink() or not md_path.is_file():
        raise BundleError("SKILL.md missing")
    skill_md = md_path.read_bytes().decode("utf-8")
    files: dict[str, str] = {}
    for sub in _SUPPORT_DIRS:
        root = skill_dir / sub
        if not root.is_dir() or root.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                fp = Path(dirpath) / fn
                if fn.startswith(".") or fp.is_symlink() or not fp.is_file():
                    continue
                rel = fp.relative_to(skill_dir).as_posix()
                try:
                    files[rel] = fp.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    continue  # binary assets are not synced
    return Bundle(skill_md, files)


def validate_bundle(bundle: Bundle) -> None:
    """Size, path and secret checks shared by push (outgoing) and pull-apply."""
    if not bundle.skill_md.strip():
        raise BundleError("SKILL.md is empty")
    if len(bundle.skill_md.encode("utf-8")) > SKILL_MD_MAX_BYTES:
        raise BundleError("SKILL.md over size limit")
    if len(bundle.files) > SKILL_BUNDLE_MAX_FILES:
        raise BundleError("too many supporting files")
    total = 0
    reason = find_secret(bundle.skill_md)
    if reason:
        raise BundleError(f"SKILL.md {reason}")
    for path, content in bundle.files.items():
        bad = check_rel_path(path)
        if bad:
            raise BundleError(f"{path}: {bad}")
        size = len(content.encode("utf-8"))
        total += size
        if size > SKILL_FILE_MAX_BYTES:
            raise BundleError(f"{path}: over file size limit")
        reason = find_secret(content)
        if reason:
            raise BundleError(f"{path} {reason}")
    if total > SKILL_BUNDLE_MAX_BYTES:
        raise BundleError("supporting files over bundle size limit")


def bundle_from_pull(item: dict) -> Bundle:
    """Build and verify a bundle from a pull payload item (untrusted).

    Checks the slug, every file ``sha256`` and the bundle ``contentHash``
    against what the payload claims, then runs :func:`validate_bundle`.
    """
    slug = item.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise BundleError("invalid slug")
    skill_md = item.get("skillMd")
    if not isinstance(skill_md, str):
        raise BundleError("skillMd missing")
    files: dict[str, str] = {}
    for f in item.get("files") or []:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("content"), str):
            raise BundleError("malformed file entry")
        path = f["path"]
        bad = check_rel_path(path)
        if bad:
            raise BundleError(f"{path}: {bad}")
        if path in files:
            raise BundleError(f"{path}: listed twice")
        claimed = f.get("sha256")
        if claimed is not None and claimed != sha256_hex(f["content"]):
            raise BundleError(f"{path}: sha256 mismatch")
        files[path] = f["content"]
    bundle = Bundle(skill_md, files)
    claimed_hash = item.get("contentHash")
    if not isinstance(claimed_hash, str) or claimed_hash != bundle.content_hash:
        raise BundleError("contentHash mismatch")
    validate_bundle(bundle)
    return bundle


def write_bundle_dir(dest: Path, bundle: Bundle) -> None:
    """Write a bundle into an (empty, new) directory."""
    dest.mkdir(parents=True, exist_ok=False)
    (dest / "SKILL.md").write_text(bundle.skill_md, encoding="utf-8", newline="")
    for rel, content in bundle.files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
