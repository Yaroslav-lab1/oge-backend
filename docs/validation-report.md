# Протокол локальной проверки

Дата: 06–07.11.2026. Среда: macOS, Python 3.12.10, SQLite; целевой стек — PostgreSQL 16, Redis 7 и Docker Compose.

| Проверка | Результат |
|---|---|
| pytest | 89 passed |
| Ruff check / format | PASS |
| mypy | PASS, 29 source files |
| OpenAPI 3.1 | PASS, 41 документированная операция |
| Coverage всего app | 81,77%; gate 80% PASS |
| SQLite migration upgrade/check/downgrade/upgrade | PASS |
| Seed математика + физика | PASS, 8 пользователей, 2 версии КИМ |
| Seed нагрузки | PASS, 2 синтетических участника на пробном cohort |
| SQL UPDATE аудита | Заблокирован DB trigger |
| Offline demo | PASS, миграции + seed + OpenAPI + 51 тест |
| Docker Compose config | PASS; Docker daemon недоступен |
| Dependency audit | FAIL: 36 записей о находках в 2 пакетах, включая повторяющиеся идентификаторы |

Полный отчёт аудита — [dependency-audit.json](dependency-audit.json). Исправленные версии не установлены: официальный индекс PyPI не ответил на запросы установки и проверки метаданных. Находки не подавляются в CI. После восстановления доступа нужно обновить FastAPI/Starlette и PyJWT совместно, закрепить полученный граф зависимостей и повторить make check, make coverage и pip-audit.

Отчёт покрытия — [coverage.xml](coverage.xml). Нагрузочные измерения описаны отдельно в [load-test-report.md](load-test-report.md); они не выполнены. PostgreSQL restore и RPO/RTO не измерялись — [backup-restore.md](backup-restore.md).
