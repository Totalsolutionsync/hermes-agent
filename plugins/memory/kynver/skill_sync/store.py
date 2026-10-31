"""Local state for skill sync: paths, atomic 0600 JSON, sidecars, skill lookup.

All files live under the active profile (``get_hermes_home()``). Sidecars hold
hashes and markers only — never bundle text, except the explicit
``.kynver.server.json`` copy kept beside a locally pending skill.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from hermes_constants import get_hermes_home

from .bundle import SERVER_COPY_NAME, SIDECAR_NAME, slug_for

try:  # POSIX only; Windows falls back to the in-process lock.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

_EXCLUDED = {".archive", ".hub", ".git", "node_modules", "__pycache__"}
_proc_lock = threading.RLock()


def skills_root() -> Path:
    return get_hermes_home() / "skills"


def archive_root() -> Path:
    return skills_root() / ".archive"


def state_dir() -> Path:
    d = get_hermes_home() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_root() -> Path:
    # Outside skills/ on purpose: Hermes indexes any SKILL.md it can reach there.
    return state_dir() / "kynver-skill-backups"


def tmp_root() -> Path:
    return state_dir() / "kynver-skill-tmp"


@contextmanager
def file_lock(name: str) -> Iterator[None]:
    """Cross-process (flock) and cross-thread lock named ``name`` under state/."""
    with _proc_lock:
        if fcntl is None:
            yield
            return
        fd = open(state_dir() / f"{name}.lock", "a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# -- global sync state (cursor, etag, last pull, rejections) -----------------

def _state_path() -> Path:
    return state_dir() / "kynver-skill-cache.json"


def load_state() -> dict[str, Any]:
    st = read_json(_state_path(), {})
    return st if isinstance(st, dict) else {}


def update_state(**changes: Any) -> dict[str, Any]:
    with file_lock("kynver-skill-cache"):
        st = load_state()
        st.update(changes)
        atomic_write_json(_state_path(), st)
        return st


# -- skill lookup and sidecars ------------------------------------------------

def locate_skill_dir(slug: str) -> Optional[Path]:
    """Live (non-archived) skill folder whose name maps to ``slug``."""
    root = skills_root()
    if not root.is_dir():
        return None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED]
        if "SKILL.md" in filenames and slug_for(Path(dirpath).name) == slug:
            return Path(dirpath)
    return None


_TS_SUFFIX = re.compile(r"-\d{14}$")


def locate_archived_dir(slug: str) -> Optional[Path]:
    """Newest ``.archive/`` folder for ``slug`` (timestamp-suffixed duplicates included)."""
    root = archive_root()
    if not root.is_dir():
        return None
    for p in sorted(root.iterdir(), reverse=True):
        if p.is_dir() and (p / "SKILL.md").is_file() and slug_for(_TS_SUFFIX.sub("", p.name)) == slug:
            return p
    return None


def read_sidecar(skill_dir: Path) -> dict[str, Any]:
    data = read_json(skill_dir / SIDECAR_NAME, {})
    return data if isinstance(data, dict) else {}


def _bases_path() -> Path:
    return state_dir() / "kynver-skill-bases.json"


def get_base(slug: str) -> Optional[str]:
    """Last hash known to match the server for ``slug`` (survives folder deletion)."""
    v = read_json(_bases_path(), {}).get(slug)
    return v if isinstance(v, str) else None


def _set_base(slug: str, value: Optional[str]) -> None:
    with file_lock("kynver-skill-bases"):
        bases = read_json(_bases_path(), {})
        if value:
            bases[slug] = value
        else:
            bases.pop(slug, None)
        atomic_write_json(_bases_path(), bases)


def _slug_of_dir(skill_dir: Path) -> str:
    return slug_for(_TS_SUFFIX.sub("", skill_dir.name))


def write_sidecar(skill_dir: Path, data: dict[str, Any]) -> None:
    atomic_write_json(skill_dir / SIDECAR_NAME, data)
    if isinstance(data.get("baseHash"), str):
        _set_base(_slug_of_dir(skill_dir), data["baseHash"])


def update_sidecar(skill_dir: Path, **changes: Any) -> dict[str, Any]:
    data = read_sidecar(skill_dir)
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    write_sidecar(skill_dir, data)
    return data


def write_server_copy(skill_dir: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(skill_dir / SERVER_COPY_NAME, payload)


def clear_server_copy(skill_dir: Path) -> None:
    try:
        (skill_dir / SERVER_COPY_NAME).unlink()
    except OSError:
        pass
