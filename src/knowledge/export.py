import os
import shutil
from src.knowledge.store import get_store


def export_bundle(out_dir: str, redact: bool = False):
    store = get_store()
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    for path, c in store.concepts.items():
        fp = os.path.join(out_dir, path + ".md")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        body = c.body
        with open(fp, "w", encoding="utf-8") as f:
            f.write("---\n")
            import yaml
            fm = dict(c.frontmatter or {})
            fm.setdefault("type", c.concept_type)
            f.write(yaml.safe_dump(fm, sort_keys=False, allow_unicode=True))
            f.write("---\n\n")
            f.write(body or "")
    # root index
    with open(os.path.join(out_dir, "index.md"), "w", encoding="utf-8") as f:
        f.write('---\nokf_version: "0.2"\n---\n\n# Index\n')
    return out_dir