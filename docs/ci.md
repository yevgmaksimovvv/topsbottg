# CI и деплой

Workflow [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) запускается при обновлении PR, при push в `main` и вручную через GitHub Actions. Набор проверок зависит от изменённых файлов. Правила находятся в [`.github/ci-paths.yml`](../.github/ci-paths.yml).

## Какие задачи запускаются

| Изменения | Проверки |
| --- | --- |
| `README.md`, `docs/**`, прочий Markdown вне специальных каталогов | Только `changes` и `checks` |
| `frontend/**` | `lint`, `browser`, `package` |
| `src/**` | `lint`, `python`, `postgres`, `package` |
| `src/topsbottg/models.py` | Как `src/**`, плюс `migrations` |
| `migrations/**` | `lint`, `python`, `postgres`, `migrations`, `package` |
| `tests/browser/**` | `lint`, `browser` |
| `tests/js/**` | `lint` (внутри него запускаются JS-тесты) |
| Обычные Python-тесты | `lint`, `python` |
| `tests/test_postgres*.py` | `lint`, `python`, `postgres` (для `test_postgres_legacy_migrations.py` также `migrations`) |
| `nginx/**`, `Dockerfile`, `docker-compose.yml` | `package` |
| `.github/**`, `AGENTS.md`, зависимости, `scripts/ci/**`, неизвестный файл | Все проверки |

При изменении нескольких частей наборы объединяются. Например, изменение Python-сервиса запускает PostgreSQL-тесты даже без изменения схемы: поведение транзакций от схемы не зависит.

Сами задачи:

- `lint` — Ruff, `node --check` для frontend и тесты `tests/js/*.test.mjs`;
- `python` — pytest без тестов с меткой `postgres`, с отчётом покрытия ветвлений;
- `postgres` — pytest с меткой `postgres` на PostgreSQL 16;
- `browser` — Playwright/Chromium и скриншоты Mini App;
- `migrations` — `alembic upgrade head` на новой PostgreSQL 16;
- `package` — проверка MIME и заголовков через Nginx, сборка Docker-образа.

Задача `changes` определяет набор проверок, а `checks` завершается успешно только тогда, когда все обязательные для изменения задачи прошли. `Skipped` у нерелевантной задачи — нормальный результат. Тесты маршрутизации находятся в `tests/js/ci-paths.test.mjs` и `tests/js/ci-selective.test.mjs`.

При ручном запуске workflow (`workflow_dispatch`) **всегда запускаются все проверки**.

## Когда происходит деплой

**После успешного CI изменения приложения в `main` выкладываются автоматически.** Изменения только Markdown (например, `README.md` или `docs/**`) не запускают деплой.

PR сам по себе не запускает деплой. Ручной запуск workflow на `main` тоже может выложить приложение — после полного CI. В задании `Deploy production` используется Environment `production` и отдельно настроенные SSH-параметры.

Скрипт сверяет SHA с текущим `origin/main`, делает резервную копию БД и выкладывает конкретную проверенную ревизию. Если `main` успел уйти вперёд, старая выкладка прекращается. Порядок запуска и состав приложения описаны в [README](../README.md).

Успешный `checks` **не означает успешный деплой**: проверяйте результат отдельной задачи `Deploy production`.

Деплой пропускается, когда выбор изменений не назначил ни одной тестовой задачи. Ручной запуск workflow на `main` по-прежнему выполняет полный CI и деплой.

## Запуск проверок локально

```bash
uv sync --locked --extra dev
uv run --locked ruff check .
uv run --locked python -m pytest -q -m "not postgres"

npm ci
node --test tests/js/*.test.mjs
npx playwright install chromium
npm run test:browser
```

PostgreSQL-тестам нужна база и `POSTGRES_TEST_URL`:

```bash
POSTGRES_TEST_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/topsbottg \
  uv run --locked python -m pytest -q -m postgres
```

Проверка миграций: `DATABASE_URL=... uv run --locked alembic upgrade head`. Используйте отдельную тестовую базу: эта команда применяет миграции.

## Ограничения проверок

Скриншоты из `browser` сохраняются как artifact на семь дней, но автоматического сравнения с эталоном нет. Отчёт о покрытии Python публикуется без обязательного порога. Прохождение Chromium не заменяет проверку в Telegram WebView на конкретном устройстве.

Для запрета слияния PR с красным CI задайте `checks` как обязательную проверку в правилах защиты `main`. Сам workflow не блокирует ручное слияние на уровне GitHub.
