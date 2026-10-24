import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from src.knowledge.store import get_store


REPO_ROOT = Path(__file__).resolve().parents[2]


def _redact_text(text):
    if not isinstance(text, str):
        return text
    text = re.sub(r"\b[\w\.-]+@[\w\.-]+\.\w+\b", "[email]", text)
    text = re.sub(r"\+?\d[\d\s-]{8,}\d", "[phone]", text)
    return text


def _redact_value(value):
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _is_safe_target(out_dir, force=False):
    if os.path.islink(out_dir):
        raise ValueError("Export target cannot be a symlink")
    target = Path(out_dir)
    resolved = target.resolve(strict=False)
    if resolved == REPO_ROOT or resolved == Path("/").resolve():
        raise ValueError("Export target cannot be the repository root or filesystem root")
    # allowed roots: current working directory and system temp
    allowed_roots = [Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve()]
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError("Export target must be under the current working directory or temp dir")
    # refuse path traversal / parent-ish
    if ".." in Path(out_dir).parts:
        raise ValueError("Export target cannot contain '..' segments")
    return resolved


def export_bundle(out_dir: str, redact: bool = False, force: bool = False):
    resolved = _is_safe_target(out_dir, force=force)
    target = Path(out_dir)

    if target.exists() or target.is_symlink():
        if not force:
            raise FileExistsError(
                f"Export target already exists: {out_dir}. Pass force=True (or --force) to overwrite."
            )
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    parent = resolved.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix=".export_", dir=str(parent))
    tmp_path = Path(tmp_dir)
    try:
        store = get_store()
        for path, c in store.concepts.items():
            fp = tmp_path / (path + ".md")
            fp.parent.mkdir(parents=True, exist_ok=True)
            body = c.body
            fm = dict(c.frontmatter or {})
            fm.setdefault("type", c.concept_type)
            if redact:
                body = _redact_text(body or "")
                fm = _redact_value(fm)
            with open(fp, "w", encoding="utf-8") as f:
                f.write("---\n")
                f.write(yaml.safe_dump(fm, sort_keys=False, allow_unicode=True))
                f.write("---\n\n")
                f.write(body or "")
        with open(tmp_path / "index.md", "w", encoding="utf-8") as f:
            f.write('---\nokf_version: "0.2"\n---\n\n# Index\n')
        # atomic rename
        tmp_path.replace(resolved)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return str(resolved)
