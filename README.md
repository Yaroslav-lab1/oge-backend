# Backend учебной платформы ОГЭ

API для учебного экзамена по математике и физике: версии КИМ, проведение сессий, автоматическая и независимая экспертная проверка, результаты, аудит и обезличенная аналитика. Проект использует только синтетические данные и не предназначен для проведения государственного экзамена.

> Статус: кандидат для локальной демонстрации. Dependency audit пока не пройден; эксплуатационные испытания не выполнены. См. [состояние реализации](docs/implementation-status.md).

## Быстрый запуск

Требуются Docker Engine и Docker Compose. Скопируйте `.env.example` в `.env`, задайте собственный `JWT_SECRET` и пароль начального администратора, затем запустите:

```bash
docker compose up --build
```

Миграции применяются при запуске API. Swagger UI: `http://localhost:8000/docs`; live: `http://localhost:8000/api/v1/health/live`; готовность: `http://localhost:8000/api/v1/health/ready`. Метрики доступны по `/api/v1/metrics` с заголовком `X-Metrics-Token`.

Синтетические демонстрационные данные:

```bash
docker compose exec api python -m scripts.seed
```

Команда печатает количество синтетических пользователей и версий пакетов. Пароль задаётся переменной `SEED_PASSWORD`; значение по умолчанию предназначено только для локальной демонстрации.

## Разработка без Docker

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
DATABASE_URL=sqlite+aiosqlite:///./oge.db .venv/bin/alembic upgrade head
.venv/bin/python -m pytest -q
```

Для целевого стенда используйте PostgreSQL 16 и Redis 7 из Compose. SQLite подходит для быстрого локального запуска и тестов; конкурентные гарантии требуют отдельной проверки на PostgreSQL.

## API и сценарий

Все прикладные маршруты имеют префикс `/api/v1`. Вход: `POST /auth/login`; далее передавайте JWT в `Authorization: Bearer <token>`. Роль и владение ресурсом проверяются на сервере. `Idempotency-Key` поддерживается для сохранения ответа, аудио, события контроля и завершения. Для сохранения ответа передавайте `expectedRevision`; устаревшая ревизия возвращает `409` с текущей ревизией.

API опубликован в [openapi.yaml](openapi.yaml). Контракт импорта и синтетические ZIP находятся в [docs/kim-import.md](docs/kim-import.md) и `examples/kim/`. Сквозной сценарий — в [docs/demo-script.md](docs/demo-script.md).

## Проверки

```bash
make check
```

Отдельные команды: `make lint`, `make typecheck`, `make test`, `make openapi`, `make migrate`. CI запускает их на PostgreSQL и Redis, проверяет спецификацию и собирает контейнер. Нагрузочный сценарий находится в `load/k6.js`; результаты измерений фиксируются в [docs/load-test-report.md](docs/load-test-report.md).

## Документация

- [Архитектура](docs/architecture.md), [ER-диаграмма](docs/erd.md), [матрица RBAC](docs/rbac-matrix.md)
- [Соглашения API](docs/api-conventions.md), [оценивание](docs/scoring.md), [безопасность](docs/security.md)
- [Резервное копирование](docs/backup-restore.md), [трассировка требований](docs/traceability.md), [состояние реализации](docs/implementation-status.md)

Внешний TLS termination, мониторинг доступности, WAL-архивация и эксплуатационные пороги RPO/RTO настраиваются на стенде. Ограничения локальной проверки перечислены в `docs/implementation-status.md`.
