from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from topsbottg.config import Settings


def _set_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_DOMAIN", "localhost")
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "123,456")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://topsbottg_app:test-password@postgres:5432/topsbottg")
    monkeypatch.setenv("MINI_APP_URL", "https://localhost/miniapp/")
    monkeypatch.setenv("BROADCAST_RATE_PER_SECOND", "5")


def _assert_invalid(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv(field, "")
    with pytest.raises(ValidationError):
        Settings()


def test_missing_app_domain_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _set_valid_env(monkeypatch)
    monkeypatch.delenv("APP_DOMAIN", raising=False)

    with pytest.raises(ValidationError):
        Settings()


def test_empty_app_domain_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _assert_invalid(monkeypatch, "APP_DOMAIN")


def test_invalid_app_domain_with_scheme_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("APP_DOMAIN", "https://bot.example.com")

    with pytest.raises(ValidationError):
        Settings()


def test_mini_app_url_host_mismatch_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("APP_DOMAIN", "bot.example.com")
    monkeypatch.setenv("MINI_APP_URL", "https://other.example.com/miniapp/")

    with pytest.raises(ValidationError):
        Settings()


def test_valid_app_domain_and_mini_app_url_creates_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DOMAIN", "bot.example.com")
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "123,456")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://topsbottg_app:test-password@postgres:5432/topsbottg")
    monkeypatch.setenv("MINI_APP_URL", "https://bot.example.com/miniapp/")
    monkeypatch.setenv("BROADCAST_RATE_PER_SECOND", "5")

    settings = Settings()

    assert settings.app_domain == "bot.example.com"
    assert settings.mini_app_url == "https://bot.example.com/miniapp/"
    assert settings.admin_ids_set == {123, 456}


def test_letsencrypt_email_is_not_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _set_valid_env(monkeypatch)
    monkeypatch.delenv("LETSENCRYPT_EMAIL", raising=False)

    settings = Settings()

    assert settings.app_domain == "localhost"


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_compose_wires_app_domain_and_keeps_email_out_of_app() -> None:
    text = _read_text("docker-compose.yml")
    assert (
        "  nginx:\n"
        "    image: nginx:1.27-alpine\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        "      APP_DOMAIN: ${APP_DOMAIN:?APP_DOMAIN is required}\n"
    ) in text
    assert "LETSENCRYPT_EMAIL: ${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL is required}" not in text


def test_compose_exposes_adminer_and_points_it_at_postgres() -> None:
    text = _read_text("docker-compose.yml")
    assert (
        "  adminer:\n"
        "    image: adminer:4\n"
        "    profiles: [\"adminer\"]\n"
        "    restart: \"no\"\n"
        "    environment:\n"
        "      ADMINER_DEFAULT_SERVER: postgres\n"
        "    depends_on:\n"
        "      postgres:\n"
        "        condition: service_healthy\n"
        "    ports:\n"
        "      - \"8080:8080\"\n"
    ) in text


def test_nginx_template_uses_app_domain() -> None:
    text = _read_text("nginx/templates/default.conf.template")
    assert "server_name ${APP_DOMAIN};" in text
    assert "ssl_certificate /etc/letsencrypt/live/${APP_DOMAIN}/fullchain.pem;" in text


def test_local_cert_bootstrap_uses_host_openssl_and_volume_copy() -> None:
    text = _read_text("scripts/generate-local-certs.sh")
    assert "command -v openssl" in text
    assert "openssl req -x509" in text
    assert ("tar -C \"$TMP_DIR\" -cf - fullchain.pem privkey.pem | "
            "docker compose --profile certbot run --rm --no-deps "
            "--entrypoint python certbot -c" in text)
    assert "archive.extractall('$LIVE_DIR')" in text


def test_issue_script_requires_email_and_uses_webroot() -> None:
    text = _read_text("scripts/issue-letsencrypt-cert.sh")
    assert ": \"${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL is required}\"" in text
    assert "APP_DOMAIN must be a real domain without scheme, port, path, or whitespace" in text
    assert "docker compose up -d --force-recreate --no-deps nginx" in text
    assert "certonly" in text and "--webroot" in text
    assert "LETSENCRYPT_STAGING" in text


def test_renew_script_only_needs_nginx_and_certbot() -> None:
    text = _read_text("scripts/renew-letsencrypt-cert.sh")
    assert "docker compose up -d --no-deps nginx" in text
    assert "docker compose --profile certbot run --rm certbot renew" in text
    assert "docker compose exec nginx nginx -s reload" in text


def test_empty_bot_token_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _assert_invalid(monkeypatch, "BOT_TOKEN")


def test_empty_admin_telegram_ids_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _assert_invalid(monkeypatch, "ADMIN_TELEGRAM_IDS")


def test_empty_database_url_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _assert_invalid(monkeypatch, "DATABASE_URL")


def test_empty_mini_app_url_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _assert_invalid(monkeypatch, "MINI_APP_URL")


def test_empty_broadcast_rate_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _assert_invalid(monkeypatch, "BROADCAST_RATE_PER_SECOND")
