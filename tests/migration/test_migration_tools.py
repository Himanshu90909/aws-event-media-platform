from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_migration_script_has_safe_stages():
    script = (ROOT / "scripts/migration/migrate.sh").read_text()
    for stage in ("preflight", "build", "smoke", "rollback"):
        assert stage in script
    assert "PREVIOUS_IMAGE" in script
    assert "docker image inspect" in script


def test_terraform_does_not_commit_state():
    assert not list((ROOT / "infrastructure/terraform").glob("*.tfstate*"))
    assert "terraform.tfstate" in (ROOT / ".dockerignore").read_text()
