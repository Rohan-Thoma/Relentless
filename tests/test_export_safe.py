
from src.knowledge.export import export_bundle


def test_export_refuses_repo_root():
    try:
        export_bundle(".", force=True)
        raised = False
    except (ValueError, FileExistsError):
        raised = True
    assert raised


def test_export_refuses_parent_traversal():
    try:
        export_bundle("../evil", force=True)
        raised = False
    except (ValueError, FileExistsError):
        raised = True
    assert raised


def test_export_redact_flag(tmp_path):
    out = tmp_path / "out"
    export_bundle(str(out), redact=True, force=True)
    # index exists
    assert (out / "index.md").exists()