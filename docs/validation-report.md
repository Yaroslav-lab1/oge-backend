# Протокол локальной проверки

Дата: 06–07.11.2026. Среда: macOS, Python 3.12.10, SQLite; целевой стек — PostgreSQL 16, Redis 7 и Docker Compose.

| Проверка | Результат |
|---|---|
| pytest | 89 passed |
| Ruff check / format | PASS |
| mypy | PASS, 29 source files |
| OpenAPI 3.1 | PASS, 41 документированная операция |
| Coverage всего app | 81,78%; gate 80% PASS |
| SQLite migration upgrade/check/downgrade/upgrade | PASS |
| Seed математика + физика | PASS, 8 пользователей, 2 версии КИМ |
| Seed нагрузки | PASS, 2 синтетических участника на пробном cohort |
| SQL UPDATE аудита | Заблокирован DB trigger |
| Offline demo | PASS, миграции + seed + OpenAPI + 51 тест |
| Docker Compose config | PASS; Docker daemon недоступен |
| Dependency audit | PASS: известных уязвимостей не найдено |

Полный отчёт аудита — [dependency-audit.json](dependency-audit.json). Найденные в первом прогоне уязвимости устранены обновлением FastAPI 0.162.0, Starlette 1.10.0 и PyJWT 2.15.1. Повторные make check и coverage прошли; pip-audit не нашёл известных уязвимостей. Все runtime-зависимости закреплены по версиям.

Отчёт покрытия — [coverage.xml](coverage.xml). Нагрузочные измерения описаны отдельно в [load-test-report.md](load-test-report.md); они не выполнены. PostgreSQL restore и RPO/RTO не измерялись — [backup-restore.md](backup-restore.md).
