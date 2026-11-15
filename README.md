# topsbottg

Telegram-бот для организации выплат: от регистрации получателей до подтверждения оплаты. Администратор работает через Mini App.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-API-009688)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED)
![License](https://img.shields.io/badge/License-MIT-green)

## Возможности

- Регистрация получателей: ФИО, учебная группа, платёжные реквизиты.
- Создание выплат и выбор получателей для рассылки.
- Сообщения и ответы через Telegram.
- Отслеживание статусов и отметка оплаты после перевода.
- История выплат и выгрузка CSV.
- Приглашение администраторов и управление их доступом.

## Mini App

Четыре вкладки: **Создать**, **Выбор**, **Получатели**, **История**.

Владельцы могут искать зарегистрированных пользователей по `@username` или Telegram ID и отправлять приглашения. Пользователь подтверждает приглашение в боте. Поиск по username использует Telegram MTProto (`contacts.resolveUsername`); ник не сохраняется в нашей базе. Перед приглашением привязка username к Telegram ID проверяется повторно.

## Быстрый запуск

```bash
cp .env.example .env
# Заполните настройки в .env
docker compose up -d --build
```

Для поиска по username нужны `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`, полученные в [Telegram API Development Tools](https://my.telegram.org). Укажите их в `.env` вместе с `BOT_TOKEN`; номер телефона или пользовательская сессия Telegram не нужны. Если параметры не заданы, поиск по Telegram ID продолжает работать, а поиск по username возвращает ошибку настройки. Не публикуйте API hash и токен.

Локально интерфейс доступен по адресу `http://127.0.0.1:18080/miniapp/`.

Для работы Mini App внутри Telegram нужен публичный HTTPS-адрес в `MINI_APP_URL` и прокси на локальный порт `18080`.

## Стек

| Компонент | Технологии |
| --- | --- |
| Бот | Python 3.12, aiogram 3 |
| API | FastAPI |
| База данных | PostgreSQL 16, SQLAlchemy, Alembic |
| Mini App | HTML, CSS, JavaScript |
| Развёртывание | Docker Compose, Nginx |

## CI/CD

GitHub Actions запускает проверки для pull requests и коммитов в `main`. Успешный push в `main` автоматически запускает деплой.

Подробности — в [docs/ci.md](docs/ci.md).

## Лицензия

MIT
