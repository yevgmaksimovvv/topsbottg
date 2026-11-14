# CI и релизы

Этот репозиторий использует пять независимых наборов проверок GitHub Actions:

- `lint`: Ruff, проверка синтаксиса JavaScript и unit-тесты JS;
- `python`: pytest на Python 3.12 без PostgreSQL-only тестов, отчёт branch coverage;
- `postgres`: настоящая PostgreSQL 16 для конкурентных проверок, API и версионности реквизитов;
- `browser`: Chromium Mini App, независимые тесты навигации и layout-регрессии, плюс детальные сценарии с мок-API;
- `package`: Alembic на PostgreSQL, проверка Nginx MIME и сборка Docker.

Job `checks` сохраняет существующее имя и завершается успешно **только если все пять наборов прошли**. В GitHub рекомендуется включить branch protection для `main` и объявить `checks` обязательным status check. Это настраивается отдельно в GitHub, а не внутри PR.

## Деплой

Push/merge в `main` выполняет CI, **но не деплой**. Для production deployment требуется отдельный ручной запуск workflow `CI` через **Actions → CI → Run workflow**, обязательно на ветке `main`. Запуск ещё раз проверит код; только затем job `Deploy production` выполнит текущую процедуру безопасной выкладки точного SHA. Права на ручной запуск и environment protection нужно ограничить через настройки GitHub.

## Запуск локально

```sh
uv sync --locked --extra dev
uv run --locked python -m pytest -q
uv run --locked ruff check .
npm ci
npx playwright install chromium
npm run test:browser
```

PostgreSQL integration-тесты выделены маркером `postgres` и запускаются в отдельном job с `POSTGRES_TEST_URL`. Для локального запуска нужен доступный PostgreSQL 16. Тест `tests/test_admin_authorization_matrix.py` автоматически обнаруживает новые `/api/admin/` endpoints, для которых не добавлена проверка прав. `tests/test_postgres_legacy_migrations.py` проверяет реальную миграцию исторических записей из старой схемы PostgreSQL до head.

## Ограничения

Сценарий `tests/browser/frontend.test.mjs` по-прежнему содержит последовательные UI workflows: часть тестов зависит от состояния предыдущих шагов. Отдельные `layout-guard.test.mjs` и `navigation-smoke.test.mjs` дают независимые проверки вёрстки и четырёх вкладок на нескольких ширинах; дальнейшую декомпозицию состояния следует выполнять без потери coverage сценариев. Скриншоты и инварианты геометрии позволяют выявлять и расследовать регрессии, но **не являются эталонным пиксельным сравнением изображений**. Полноценные screenshot goldens требуют принятия и хранения эталонов в том же runner-окружении.

Coverage собирается без минимального порога: главное сначала иметь данные о реально непроверенных ветках. Он не должен заставлять писать бессодержательные тесты ради процентов.
