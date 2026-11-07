from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-production.sh"
CI_DEPLOY_SCRIPT = ROOT / "scripts" / "ci-deploy.sh"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="Deployment integration harness runs on Linux CI.")


def _production_checkout(tmp_path: Path, *, backup_fails: bool = False) -> tuple[Path, str, Path]:
    project = tmp_path / "apps" / "topsbottg"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "deploy@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Deploy test"], check=True)
    (project / "tracked.txt").write_text("old backend\n", encoding="utf-8")
    frontend = project / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("old frontend\n", encoding="utf-8")
    compose = """services:
  postgres:
    image: postgres:16
    volumes: [postgres_data:/var/lib/postgresql/data]
  app:
    build: .
  nginx:
    image: nginx:alpine
    volumes: [./frontend:/usr/share/nginx/html/miniapp:ro]
volumes:
  postgres_data:
"""
    (project / "docker-compose.yml").write_text(
        compose.replace("build: .", "image: example.invalid/old-app"), encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(project), "add", "tracked.txt", "frontend/index.html", "docker-compose.yml"], check=True
    )
    subprocess.run(["git", "-C", str(project), "commit", "-m", "deployed version"], check=True, capture_output=True)

    (project / "tracked.txt").write_text("new backend\n", encoding="utf-8")
    (frontend / "index.html").write_text("new frontend\n", encoding="utf-8")
    (project / "docker-compose.yml").write_text(compose, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(project), "add", "tracked.txt", "frontend/index.html", "docker-compose.yml"], check=True
    )
    subprocess.run(["git", "-C", str(project), "commit", "-m", "release candidate"], check=True, capture_output=True)
    sha = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", "https://github.com/yevgmaksimovvv/topsbottg.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(project), "update-ref", "refs/remotes/origin/main", sha], check=True)
    subprocess.run(["git", "-C", str(project), "checkout", "HEAD^"], check=True, capture_output=True)
    (project / ".env").write_text("APP_DOMAIN=example.com\nCOMPOSE_PROJECT_NAME=custom-topsbottg\n", encoding="utf-8")
    backup_dir = project / "scripts"
    backup_dir.mkdir()
    backup_script = "#!/usr/bin/env bash\nset -e\n"
    if backup_fails:
        backup_script += "exit 1\n"
    else:
        backup_script += (
            "mkdir -p backups; printf 'SQL\\n' > backups/backup.sql; "
            "echo 'Backup completed: backups/backup.sql'\n"
        )
    backup_file = backup_dir / "backup.sh"
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
        "if [[ $* == *' config --quiet' ]]; then\n"
        "  \"$REAL_DOCKER\" \"$@\" || exit $?\n"
        "  args=(\"$@\"); unset 'args[-1]'; args+=(--format json); "
        "\"$REAL_DOCKER\" \"${args[@]}\" > \"$COMPOSE_RESOLVED_FILE\" || exit $?\n"
        "fi\n"
        "if [[ $* == *' config --format json' ]]; then \"$REAL_DOCKER\" \"$@\"; exit $?; fi\n"
        "if [[ ${FAIL_APP_START:-0} == 1 && $* == *'up -d --no-deps --wait'* && $* == *' app'* ]]; then exit 1; fi\n"
        "if [[ $* == *' logs --tail=80 app nginx'* ]]; then echo 'SECRET_PRODUCTION_LOG_LINE_DO_NOT_EMIT'; fi\n"
        "if [[ $* == *'alembic current'* ]]; then echo 'abc123 (head)'; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> \"$CURL_CALL_LOG\"\n"
        "if [[ ${FAIL_HEALTH:-0} == 1 && $* == *'http://127.0.0.1:18080/healthz'* ]]; then exit 22; fi\n"
        "if [[ $* == *'http://127.0.0.1:18080/miniapp/'* && $* != *'/app.js'* ]]; then\n"
        "  echo '<script type=module src=\"/miniapp/app.js?v=1\"></script>'; exit 0\n"
        "fi\n"
        "while (($#)); do\n"
        "  if [[ $1 == -D ]]; then\n"
        "    shift; { echo 'Content-Type: application/javascript; charset=utf-8'; "
        "echo 'X-Content-Type-Options: nosniff'; } > \"$1\";\n"
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
    real_docker = shutil.which("docker")
    env = os.environ | extra_env | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROJECT_DIR": str(project),
        "DOCKER_CALL_LOG": str(tmp_path / "docker.log"),
        "COMPOSE_RESOLVED_FILE": str(tmp_path / "compose-resolved.json"),
        "REAL_DOCKER": real_docker or "docker",
        "CURL_CALL_LOG": str(tmp_path / "curl.log"),
    }
    return subprocess.run(["bash", str(DEPLOY_SCRIPT), sha], env=env, text=True, capture_output=True, timeout=30)


def test_deploy_rejects_invalid_sha_before_touching_project() -> None:
    result = subprocess.run(["bash", str(DEPLOY_SCRIPT), "not-a-sha"], text=True, capture_output=True, timeout=5)
    assert result.returncode != 0
    assert "40-character" in result.stderr


def test_deploy_rejects_valid_but_stale_sha(tmp_path: Path) -> None:
    project, _, bin_dir = _production_checkout(tmp_path)
    result = _run_deploy(tmp_path, project, "0" * 40, bin_dir)
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


def test_vps_flock_rejects_concurrent_deploy_before_compose_changes(tmp_path: Path) -> None:
    import fcntl

    project, sha, bin_dir = _production_checkout(tmp_path)
    lock_path = project / ".deploy.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run_deploy(tmp_path, project, sha, bin_dir)
    assert result.returncode != 0
    assert "Another production deployment is active" in result.stderr
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


def test_app_health_failure_stops_before_frontend_switch_without_leaking_logs(tmp_path: Path) -> None:
    project, sha, bin_dir = _production_checkout(tmp_path)
    result = _run_deploy(tmp_path, project, sha, bin_dir, FAIL_APP_START="1")
    assert result.returncode != 0
    assert "SECRET_PRODUCTION_LOG_LINE_DO_NOT_EMIT" not in result.stdout + result.stderr
    assert "diagnostics are retained on VPS" in result.stderr
    failure_log = project / ".deploy-failures.log"
    assert failure_log.exists()
    assert "SECRET_PRODUCTION_LOG_LINE_DO_NOT_EMIT" in failure_log.read_text(encoding="utf-8")
    assert failure_log.stat().st_mode & 0o777 == 0o600
    assert (project / "frontend/index.html").read_text(encoding="utf-8") == "old frontend\n"
    assert subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip() != sha
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert " app" in calls
    assert not [line for line in calls.splitlines() if "up -d" in line and line.endswith("nginx")]
    assert not (project / ".deployed-sha").exists()
    assert len([line for line in calls.splitlines() if "up -d" in line and line.endswith("app")]) == 1


def test_endpoint_health_failure_fails_release_after_nginx_check(tmp_path: Path) -> None:
    project, sha, bin_dir = _production_checkout(tmp_path)
    result = _run_deploy(tmp_path, project, sha, bin_dir, FAIL_HEALTH="1")
    assert result.returncode != 0
    assert "endpoint-verification" in result.stderr
    assert not (project / ".deployed-sha").exists()
    assert (project / "frontend/index.html").read_text(encoding="utf-8") == "new frontend\n"
    docker_calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    nginx_calls = [line for line in docker_calls.splitlines() if "up -d" in line and line.endswith("nginx")]
    assert len(nginx_calls) == 1
    assert "--no-deps --force-recreate" in nginx_calls[0]


def test_deploy_happy_path_records_sha_and_recreates_only_nginx(tmp_path: Path) -> None:
    project, sha, bin_dir = _production_checkout(tmp_path)
    result = _run_deploy(tmp_path, project, sha, bin_dir)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (project / ".deployed-sha").read_text(encoding="utf-8").strip() == sha
    assert (project / "frontend/index.html").read_text(encoding="utf-8") == "new frontend\n"

    docker_calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    release_file = f"--file {tmp_path}/apps/.topsbottg-deploy/releases/{sha}/docker-compose.yml"
    for line in docker_calls.splitlines():
        if any(token in line for token in (" config --quiet", " build app", "up -d", "alembic current")):
            assert release_file in line
            assert "--project-name custom-topsbottg" in line
    assert f"--file {project}/docker-compose.yml config --format json" in docker_calls
    assert "--force-recreate" in docker_calls
    assert "--no-deps --force-recreate" in docker_calls
    nginx_up_calls = [line for line in docker_calls.splitlines() if "up -d" in line and line.endswith("nginx")]
    assert len(nginx_up_calls) == 1
    assert "--no-deps --force-recreate" in nginx_up_calls[0]
    assert " postgres" not in nginx_up_calls[0]
    assert " app" not in nginx_up_calls[0]
    compose_up_calls = [line for line in docker_calls.splitlines() if "up -d" in line]
    assert len(compose_up_calls) == 2
    assert any(line.endswith(" app") for line in compose_up_calls)

    curl_calls = (tmp_path / "curl.log").read_text(encoding="utf-8")
    assert "http://127.0.0.1:18080/healthz" in curl_calls
    assert "http://127.0.0.1:18080/miniapp/" in curl_calls
    assert "http://127.0.0.1:18080/miniapp/app.js?v=1" in curl_calls
    resolved = json.loads((tmp_path / "compose-resolved.json").read_text(encoding="utf-8"))
    release_dir = tmp_path / "apps" / ".topsbottg-deploy" / "releases" / sha
    assert resolved["services"]["app"]["build"]["context"] == str(release_dir)
    assert resolved["services"]["nginx"]["volumes"][0]["source"] == str(release_dir / "frontend")
    assert resolved["volumes"]["postgres_data"]["name"] == "custom-topsbottg_postgres_data"
    assert resolved["networks"]["default"]["name"] == "custom-topsbottg_default"


def test_ci_bootstraps_deploy_script_on_first_run_with_strict_host_checking(tmp_path: Path) -> None:
    project, sha, bin_dir = _production_checkout(tmp_path)
    remote_home = tmp_path / "remote-home"
    remote_home.mkdir()
    remote_script = remote_home / ".topsbottg-deploy-42-1.sh"
    assert not remote_script.exists()

    (bin_dir / "scp").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"scp $*\" >> \"$SSH_CALL_LOG\"\n"
        "[[ $* == *'StrictHostKeyChecking=yes'* ]] || exit 81\n"
        "for arg in \"$@\"; do if [[ $arg == UserKnownHostsFile=* ]]; then "
        "grep -Fq '[vps.example.invalid]:2222 ssh-ed25519 AAAATEST' \"${arg#*=}\" || exit 84; fi; done\n"
        "while (($#)); do case $1 in -q) shift;; -i|-o|-P) shift 2;; *) break;; esac; done\n"
        "source_file=$1; target=${2#*:}; cp \"$source_file\" \"$REMOTE_HOME/${target##*/}\"\n",
        encoding="utf-8",
    )
    (bin_dir / "ssh").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"ssh $*\" >> \"$SSH_CALL_LOG\"\n"
        "[[ $* == *'StrictHostKeyChecking=yes'* ]] || exit 82\n"
        "for arg in \"$@\"; do if [[ $arg == UserKnownHostsFile=* ]]; then "
        "grep -Fq '[vps.example.invalid]:2222 ssh-ed25519 AAAATEST' \"${arg#*=}\" || exit 85; fi; done\n"
        "remote_command=${@: -1}\n"
        "[[ $remote_command == *\"$REMOTE_SHA\"* ]] || exit 83\n"
        "cat > \"$REMOTE_HOME/remote-runner.sh\"\n"
        "bash \"$REMOTE_HOME/remote-runner.sh\" \"$REMOTE_HOME/.topsbottg-deploy-42-1.sh\" \"$REMOTE_SHA\"\n",
        encoding="utf-8",
    )
    (bin_dir / "scp").chmod(0o755)
    (bin_dir / "ssh").chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROJECT_DIR": str(project),
        "DOCKER_CALL_LOG": str(tmp_path / "docker.log"),
        "CURL_CALL_LOG": str(tmp_path / "curl.log"),
        "SSH_CALL_LOG": str(tmp_path / "ssh.log"),
        "REMOTE_HOME": str(remote_home),
        "REMOTE_SHA": sha,
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "DEPLOY_HOST": "vps.example.invalid",
        "DEPLOY_PORT": "2222",
        "DEPLOY_USER": "deploy",
        "DEPLOY_KNOWN_HOSTS": "[vps.example.invalid]:2222 ssh-ed25519 AAAATEST",
        "SSH_PRIVATE_KEY": "test-only-private-key",
    }
    result = subprocess.run(
        ["bash", str(CI_DEPLOY_SCRIPT), sha], env=env, text=True, capture_output=True, timeout=40
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert (project / ".deployed-sha").read_text(encoding="utf-8").strip() == sha
    assert not remote_script.exists(), "remote bootstrap script should be removed after execution"
    ssh_calls = (tmp_path / "ssh.log").read_text(encoding="utf-8")
    assert "StrictHostKeyChecking=yes" in ssh_calls
    assert "UserKnownHostsFile=" in ssh_calls
    assert "2222" in ssh_calls
