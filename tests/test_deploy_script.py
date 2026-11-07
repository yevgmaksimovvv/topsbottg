from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-production.sh"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="Deployment integration harness runs on Linux CI.")


def _production_checkout(tmp_path: Path, *, backup_fails: bool = False) -> tuple[Path, str, Path]:
    project = tmp_path / "production"
    project.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "deploy@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Deploy test"], check=True)
    tracked = project / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-m", "initial"], check=True, capture_output=True)
    sha = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", "https://github.com/yevgmaksimovvv/topsbottg.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(project), "update-ref", "refs/remotes/origin/main", sha], check=True)
    (project / ".env").write_text("APP_DOMAIN=example.com\n", encoding="utf-8")
    backup = project / "scripts"
    backup.mkdir()
    backup_script = "#!/usr/bin/env bash\nset -e\n"
    if backup_fails:
        backup_script += "exit 1\n"
    else:
        backup_script += (
            "mkdir -p backups; printf 'SQL\\n' > backups/backup.sql; "
            "echo 'Backup completed: backups/backup.sql'\n"
        )
    backup_file = backup / "backup.sh"
    backup_file.write_text(backup_script, encoding="utf-8")
    backup_file.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_git = subprocess.check_output(["which", "git"], text=True).strip()
    (bin_dir / "git").write_text(
        f"#!/usr/bin/env bash\nif [[ $1 == fetch ]]; then exit 0; fi\nexec {real_git} \"$@\"\n", encoding="utf-8"
    )
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> \"$DOCKER_CALL_LOG\"\n"
        "if [[ $* == *'alembic current'* ]]; then echo 'abc123 (head)'; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "if [[ ${FAIL_HEALTH:-0} == 1 && $* == *'/healthz'* ]]; then exit 22; fi\n"
        "if [[ $* == *'example.com/miniapp/'* && $* != *'/app.js'* ]]; then\n"
        "  echo '<script type=module src=\"/miniapp/app.js?v=1\"></script>'; exit 0\n"
        "fi\n"
        "while (($#)); do\n"
        "  if [[ $1 == -D ]]; then\n"
        "    shift; printf 'Content-Type: application/javascript; charset=utf-8\\nX-Content-Type-Options: nosniff\\n' "
        "> \"$1\";\n"
        "  fi; shift;\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for executable in (bin_dir / "git", bin_dir / "docker", bin_dir / "curl"):
        executable.chmod(0o755)
    return project, sha, bin_dir


def _run_deploy(
    tmp_path: Path, project: Path, sha: str, bin_dir: Path, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    call_log = tmp_path / "docker.log"
    env = os.environ | extra_env | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROJECT_DIR": str(project),
        "DOCKER_CALL_LOG": str(call_log),
    }
    return subprocess.run(["bash", str(SCRIPT), sha], env=env, text=True, capture_output=True, timeout=30)


def test_deploy_rejects_invalid_sha_before_touching_project(tmp_path: Path) -> None:
    result = subprocess.run(["bash", str(SCRIPT), "not-a-sha"], text=True, capture_output=True, timeout=5)
    assert result.returncode != 0
    assert "40-character" in result.stderr


def test_deploy_rejects_valid_but_stale_sha(tmp_path: Path) -> None:
    project, sha, bin_dir = _production_checkout(tmp_path)
    stale_sha = "0" * 40
    assert stale_sha != sha
    result = _run_deploy(tmp_path, project, stale_sha, bin_dir)
    assert result.returncode != 0
    assert "Refusing stale deploy" in result.stderr
    assert "build app" not in (tmp_path / "docker.log").read_text(encoding="utf-8")


def test_deploy_rejects_tracked_local_changes(tmp_path: Path) -> None:
    project, sha, bin_dir = _production_checkout(tmp_path)
    (project / "tracked.txt").write_text("local edit\n", encoding="utf-8")
    result = _run_deploy(tmp_path, project, sha, bin_dir)
    assert result.returncode != 0
    assert "Tracked production files" in result.stderr
    assert not (tmp_path / "docker.log").exists()


def test_backup_failure_stops_before_build_or_migration(tmp_path: Path) -> None:
    project, sha, bin_dir = _production_checkout(tmp_path, backup_fails=True)
    result = _run_deploy(tmp_path, project, sha, bin_dir)
    assert result.returncode != 0
    assert "Backup script failed" in result.stderr
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "pg_isready" in calls
    assert "build app" not in calls
    assert "alembic current" not in calls


def test_health_check_failure_does_not_record_successful_sha(tmp_path: Path) -> None:
    project, sha, bin_dir = _production_checkout(tmp_path)
    result = _run_deploy(tmp_path, project, sha, bin_dir, FAIL_HEALTH="1")
    assert result.returncode != 0
    assert "FAILED" in result.stderr
    assert not (project / ".deployed-sha").exists()
