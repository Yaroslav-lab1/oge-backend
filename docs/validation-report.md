# Протокол проверки

Дата: 06–07.11.2026. Среда: macOS, Python 3.12.10, SQLite; целевой стек — PostgreSQL 16, Redis 7 и Docker Compose.

| Проверка | Результат |
|---|---|
| pytest локально | 89 passed, 1 PostgreSQL test skipped |
| PostgreSQL concurrency в CI | PASS: параллельные start/save/claim/finalize |
| PostgreSQL migration round trip + повторный seed | PASS в CI |
| Docker build и non-root runtime | PASS в CI |
| API restart: подтверждённый ответ и ревизия сохранены | PASS в CI |
| PostgreSQL backup/restore: строки и checksum ответов | PASS в CI |
| Ruff check / format | PASS |
| mypy | PASS, 29 source files |
| OpenAPI 3.1 | PASS, 41 документированная операция |
| Coverage всего app | 81,58%; gate 80% PASS |
| SQLite migration upgrade/check/downgrade/upgrade | PASS |
| Seed математика + физика | PASS, 8 пользователей, 2 версии КИМ |
| Seed нагрузки | PASS, 2 синтетических участника на пробном cohort |
| SQL UPDATE аудита | Заблокирован DB trigger |
| Offline demo | PASS, миграции + seed + OpenAPI + 51 тест |
| Docker Compose config | PASS; Docker daemon недоступен |
| Dependency audit | PASS: известных уязвимостей не найдено |

Полный отчёт аудита — [dependency-audit.json](dependency-audit.json). Найденные в первом прогоне уязвимости устранены обновлением FastAPI 0.162.0, Starlette 1.10.0 и PyJWT 2.15.1. Повторные make check и coverage прошли; pip-audit не нашёл известных уязвимостей. Все runtime-зависимости закреплены по версиям.

Отчёт покрытия — [coverage.xml](coverage.xml). Нагрузочные измерения описаны отдельно в [load-test-report.md](load-test-report.md); они не выполнены. PostgreSQL restore проверен в CI; эксплуатационные RPO/RTO не подтверждены — [backup-restore.md](backup-restore.md).

Полный [CI run 42395589770](https://github.com/Yaroslav-lab1/oge-backend/actions/runs/42395589770) прошёл для кода 5e3efef: quality и image — success. Отдельный PostgreSQL тест проверяет сериализацию конкурентных транзакций на реальной БД.
