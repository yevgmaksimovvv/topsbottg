# CI и релизы

## Selective CI

Pull Request и push в `main` всегда запускают workflow `CI`, но тяжёлые jobs
выполняются **только для затронутых компонентов**. Файлы и зависимости
описаны в [`.github/ci-paths.yml`](../.github/ci-paths.yml). Изменения
определяет `dorny/paths-filter@v4`.

| Изменение | Запускаются |
| --- | --- |
| Только `README.md` или `docs/**` | `changes` и `checks` |
| `frontend/**` | `lint`, `browser`, `package` |
| `src/**` (кроме доп. зависимостей) | `lint`, `python`, `postgres`, `package` |
| `src/topsbottg/models.py` | как backend + `migrations` |
| `migrations/**` | `lint`, `python`, `postgres`, `migrations`, `package` |
| `tests/browser/**` | `lint`, `browser` |
| `tests/js/**` | `lint` |
| `nginx/**` или `Dockerfile` | `package` |
| `.github/**`, `uv.lock`, `pyproject.toml`, `package*.json`, `AGENTS.md` | **Все jobs** |
| Любой неизвестный путь | **Все jobs** (fail-safe) |
| Ручной `workflow_dispatch` перед production | **Все jobs**, независимо от diff |

При смешанных изменениях запускается объединение необходимых jobs.
Backend-код запускает PostgreSQL integration даже без правки схемы:
транзакционные инварианты могут зависеть от сервисной логики.
`package` **не** поднимает PostgreSQL: проверки миграций выделены в `migrations`.

### Состав проверок

- `changes` — определяет затронутые компоненты; ошибка определения блокирует PR;
- `lint` — Ruff, синтаксис JavaScript, JS unit-тесты;
- `python` — pytest без PostgreSQL-only, branch coverage с артефактом XML;
- `postgres` — PostgreSQL 16: реальная API-проверка, блокировки и конкурентность;
- `browser` — Chromium: Mini App, адаптивность и независимые smoke-тесты;
- `migrations` — чистое `alembic upgrade head` на PostgreSQL 16;
- `package` — Nginx MIME и сборка Docker без PostgreSQL;
- `checks` — **постоянный статус для branch protection**.

`checks` пропускает лишь те jobs, которые не нужны согласно `changes`.
Если нужный job пропущен, упал, отменён или список изменений не определён,
`checks` завершается ошибкой. Недостаточно просто проверить, что
«всё зелёное»: GitHub сам считает skipped job успешным для branch protection.
Эта логика проверяется в `tests/js/ci-selective.test.mjs`.

В GitHub Settings для `main` нужно сделать `checks` обязательным
Required Status Check и настроить защиту production environment.
Это изменение настроек репозитория выполняется отдельно, не кодом PR.

## Деплой

Push/merge в `main` выполняет selective CI, **но не деплой**.
Production deployment доступен только после ручного
**Actions → CI → Run workflow** на `main`. При ручном запуске `changes`
принудительно выбирает **все** проверки; после успешного `checks` может
выполниться `Deploy production` с защитой GitHub environment.
Без явного разрешения на deploy этот workflow не запускать.

## Запуск локально

```sh
uv sync --locked --extra dev
uv run --locked python -m pytest -q -m "not postgres"
uv run --locked ruff check .
npm ci
node --test tests/js/*.test.mjs
npx playwright install chromium
npm run test:browser
```

PostgreSQL integration-тесты имеют маркер `postgres` и требуют
`POSTGRES_TEST_URL`. `tests/test_admin_authorization_matrix.py` проверяет
авторизацию каждого admin endpoint. `tests/test_postgres_legacy_migrations.py`
проверяет миграцию исторических записей на PostgreSQL до `head`.

## Ограничения

Старый `tests/browser/frontend.test.mjs` остаётся большим тестом с общим
состоянием. `layout-guard.test.mjs` и `navigation-smoke.test.mjs`
запускаются независимо. Browser screenshots и геометрические проверки
не являются пиксельными golden snapshots. Метрика branch coverage собирается
без искусственного порога.

Пока этот PR изменяет сами workflow-файлы, его `full`-фильтр запускает
все jobs. Проверки selective policy для чистых документационных/frontend/backend
диффов покрыты unit-тестами агрегатора; полноценный GitHub PR-only прогон с
такими diff следует дополнительно проверить после merge, не обходя защиту `main`.
