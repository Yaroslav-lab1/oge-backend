# Трассировка требований ТЗ v1.0

Источник: `ТЗ_Backend_платформы_ОГЭ_v1.0.docx` от 01.10.2026. Дополнения по импорту КИМ и поставке взяты из `OGE_BACKEND_MASTER_PROMPT.md`; они перечислены после матрицы. Статус ТЗ в исходном документе — «На согласование».

**Статусы:** ✅ — код и указанный сценарий проверены локально; ◐ — реализована и проверена только часть условия либо есть конкретный пробел; ○ — измерение или испытание ещё не проводилось. ✅ не означает проверку на production, PostgreSQL или под нагрузкой.

Срез проверки 06.11.2026: 89 тестов прошли; Ruff, mypy и OpenAPI прошли. Покрытие всего `app` — 81,77%. Миграции и offline-сценарий проверены на SQLite. Подробности и блокировки — в [validation-report.md](validation-report.md).

## Функциональные требования

| ID | Статус | Реализация и проверяемое свидетельство |
| --- | --- | --- |
| FR-01 | ✅ | Вход, Argon2id, выдача двух токенов и `LOGIN_SUCCESS` в `app/routers/auth.py`, `app/auth.py`; `tests/test_auth.py::test_login_refresh_rotation_logout_and_profile`. |
| FR-02 | ✅ | Hash refresh, отзыв и ротация в `app/routers/auth.py::refresh`; тот же интеграционный тест проверяет отказ старому токену. |
| FR-03 | ✅ | Создание, блокировка и смена ролей с аудитом в `app/routers/admin.py`; `tests/test_auth.py::test_admin_validation_password_reset_and_live_role_revocation`. |
| FR-04 | ✅ | Ролевые зависимости `app/auth.py::require_roles`, проверка владельца в `app/session.py`, `app/routers/results.py`, назначенного эксперта в `app/routers/review.py`; отрицательные тесты в `test_auth.py`, `test_session.py`, `test_review_results.py`. Полнота всех маршрутов отдельно оценивается в NFR-10. |
| FR-05 | ✅ | Черновые `TaskVersion` в `app/routers/content.py::create_task_version`; `tests/test_content.py::test_published_snapshot_and_variant_order`. |
| FR-06 | ✅ | Валидация и публикация версии в `app/routers/content.py::publish_task_version`; тесты `test_published_snapshot_and_variant_order`, `test_unpublished_task_cannot_enter_variant`. |
| FR-07 | ✅ | `ExamVariantTask` фиксирует ID версий, опубликованная версия защищена от изменения в `app/models/content.py`; `test_published_snapshot_and_variant_order`. |
| FR-08 | ✅ | Экзамен с окном и назначениями в `app/routers/content.py`; проверка окна/назначения в `app/session.py`; `test_exam_binds_specific_scoring_rule_version`, `test_assignment_requires_student_role_and_is_unique`, `test_only_assigned_student_can_start_and_resume`. |
| FR-09 | ✅ | Одна активная сессия, серверный deadline и частичный уникальный индекс в `app/session.py`, `app/models/session.py`; `test_only_assigned_student_can_start_and_resume`, `test_database_allows_only_one_active_attempt_per_assignment`. |
| FR-10 | ✅ | Состояние сессии включает `playCount`, `remainingPlays`, задания, ответы и остаток времени; `app/session.py::_view`, тесты настраиваемого аудиолимита в `tests/test_session.py`. |
| FR-11 | ✅ | Сохранение `Answer`, `AnswerRevision` и монотонной ревизии в `app/session.py::save_answer`; `test_answer_revision_and_idempotency`. |
| FR-12 | ✅ | Конфликт возвращает `409` и `currentRevision`; `app/session.py::save_answer`, `test_answer_revision_and_idempotency`. |
| FR-13 | ✅ | Лимит хранится в неизменяемом `TaskVersion.body.audioPlayLimit`, диапазон 1..10, default 2; `app/models/content.py`, `app/session.py`; тесты лимитов 1/2/3. |
| FR-14 | ✅ | Третий запрос отклоняется, DB `CHECK` не допускает отрицательный счётчик; `app/models/session.py::AudioPlayback`, `test_audio_is_limited_signed_and_single_use`. |
| FR-15 | ✅ | `ProctorEvent` и аудит без влияния на балл в `app/session.py::add_proctor_event`; `test_proctor_event_is_append_only_and_expiry_is_scored`. |
| FR-16 | ✅ | `submit_session` закрывает работу и запускает `process_submission` один раз; `test_submission_scores_once_and_closes_answers`. |
| FR-17 | ✅ | Lifespan запускает worker с повтором после временных ошибок; `app/main.py`, `test_lifespan_expires_session_without_request` в `tests/test_session.py`. |
| FR-18 | ✅ | `app/scoring.py::process_submission` создаёт `AutoCheckResult` с баллом, ID и версией правила, исходный `Answer.raw` сохраняется; `tests/test_scoring.py::test_submission_is_idempotent_and_recalculation_preserves_result_versions`. |
| FR-19 | ✅ | Настраиваемая нормализация текста/числа/единиц в `app/scoring.py::check_answer`; `test_numeric_comma_dot_and_trailing_zero_are_equivalent`, `test_physics_unit_is_checked_without_implicit_conversion`, `test_text_normalization_does_not_mutate_original_answer`. |
| FR-20 | ✅ | Две обезличенные `ReviewAssignment` на развёрнутый ответ; `app/scoring.py::process_submission`, `app/routers/review.py::review_queue`, `test_independent_review_dispute_access_and_versioned_result`. |
| FR-21 | ✅ | Claim с блокировкой строки и ограниченным lease в `app/routers/review.py::claim_review`; `test_claim_is_unique_until_lease_expires_and_old_owner_loses_access`. Межпроцессная гонка на PostgreSQL отдельно не измерена. |
| FR-22 | ✅ | `ScoresBody` принимает баллы и комментарии по критериям; `ReviewScore.comment` хранит комментарий, неизвестные критерии отклоняются. Диапазоны и ownership проверены `tests/test_review_results.py`. |
| FR-23 | ✅ | Две независимые проверки и создание `ReviewDispute` при превышении порога в `app/scoring.py::create_result_if_ready`; `test_independent_review_dispute_access_and_versioned_result`. |
| FR-24 | ✅ | Старший эксперт задаёт `scores` по всем критериям; сохраняются `final_criteria`, итог, автор и время, затем новая версия результата; `tests/test_review_results.py`. |
| FR-25 | ✅ | Сумма и разбивка формируются после всех проверок в `app/scoring.py::create_result_if_ready`; `test_independent_review_dispute_access_and_versioned_result`, `test_submission_is_idempotent_and_recalculation_preserves_result_versions`. |
| FR-26 | ✅ | Геометрический порог из `ScoringRuleVersion` применяется в `app/scoring.py::grade_for_score`; `test_geometry_boundary_one_and_two`. |
| FR-27 | ✅ | Оценка 2–5, монотонные пороги, причина снижения и версия правила в `app/scoring.py`, `app/models/review.py::ResultVersion`; `test_geometry_boundary_one_and_two`, `test_grade_never_decreases_when_primary_score_increases`. |
| FR-28 | ✅ | Ответ результата содержит `checkingStatus`, итог, детализацию и версии правил; доступ владельца проверен `tests/test_review_results.py`. |
| FR-29 | ✅ | Пересчёт создаёт следующий `ResultVersion`, прежний доступен через `/history`; `app/routers/results.py`, `test_independent_review_dispute_access_and_versioned_result`. |
| FR-30 | ✅ | Фильтры action/actor_id/object_id/since/until и пагинация; ORM и DB trigger запрещают UPDATE/DELETE аудита. Trigger проверен на мигрированной SQLite; PostgreSQL требует запуска CI. |
| FR-31 | ✅ | Роль `BI_SERVICE`, HMAC-псевдоним и агрегаты без логинов/ответов в `app/routers/results.py::export_results`; `test_bi_export_exposes_only_anonymized_latest_result_data`. |
| FR-32 | ✅ | Раздельные liveness/readiness, проверка БД в `app/routers/ops.py`; `tests/test_ops_openapi.py::test_health_separates_liveness_from_database_readiness`. |
| FR-33 | ✅ | Структурированные логи не содержат тело запроса и неизвестный путь; `test_request_logging_excludes_body_and_unmatched_path_secrets`. |
| FR-34 | ✅ | Единый `ApiError`, обработчики 404/422/500 и correlation ID в `app/main.py`; `tests/test_ops_openapi.py::test_success_validation_auth_and_missing_route_share_correlation_contract`. |
| FR-35 | ✅ | Новые экзамены автоматически выбирают новейшую действующую шкалу на opensAt; явный устаревший ID отклоняется, старый экзамен остаётся привязан к прежней версии; `tests/test_content.py`. |
| FR-36 | ✅ | `IdempotencyRecord` с hash запроса и сохранённым ответом для answer/audio/proctor/submit в `app/session.py`; `test_answer_revision_and_idempotency`, `test_audio_is_limited_signed_and_single_use`, `test_proctor_event_is_append_only_and_expiry_is_scored`. |

## Нефункциональные требования

| ID | Статус | Реализация и проверяемое свидетельство / пробел |
| --- | --- | --- |
| NFR-01 | ○ | `/health` и Prometheus метрики есть в `app/routers/ops.py`, но 99,5% uptime в экзаменационное окно не измерялись. |
| NFR-02 | ○ | Сценарий `load/k6.js` подготовлен; нет отчёта p95 чтения при 300 одновременных сессиях. |
| NFR-03 | ○ | Сценарий `load/k6.js` подготовлен; нет отчёта p95 сохранения при 50 RPS. |
| NFR-04 | ○ | Сценарий `load/k6.js` подготовлен; 300 стартов/60 с и доля 5xx не измерялись. |
| NFR-05 | ○ | `Answer` сохраняется в SQL транзакции (`app/session.py`), но fault test с рестартом экземпляра и проверкой подтверждённого ответа не проводился. |
| NFR-06 | ○ | `scripts/backup_restore_test.sh` существует; RPO ≤5 мин и RTO ≤30 мин не измерены, Docker daemon недоступен. |
| NFR-07 | ◐ | Argon2id salted hash проверен `tests/test_auth.py::test_argon2id_password_hash_and_mismatch`. TLS 1.2+ описан в `docs/security.md` как reverse proxy, его конфигурация/сканирование в репозитории отсутствуют. |
| NFR-08 | ✅ | Ограничение срока access/refresh и ротация в `app/auth.py`, `app/routers/auth.py`; `test_login_refresh_rotation_logout_and_profile`. |
| NFR-09 | ✅ | DB `LoginAttempt`, 5 неудач/10 мин и блокировка 15 мин; `test_five_failures_in_ten_minutes_block_for_fifteen_minutes`, `test_five_bad_logins_trigger_rate_limit`. |
| NFR-10 | ◐ | `docs/rbac-matrix.md`, `require_roles` и IDOR тесты охватывают ключевые маршруты; нет автоматического отчёта, доказывающего серверные проверки роли и владения для **100%** защищённых операций. |
| NFR-11 | ◐ | События перечисленных действий создаются через `app/audit.py::record_audit`; чтение проверено `test_independent_review_dispute_access_and_versioned_result`. Хранение ≥180 суток не проверено, retention/архивная политика не реализована. |
| NFR-12 | ✅ | Middleware проставляет `X-Correlation-ID` на ответы, Prometheus формат проверен в `tests/test_ops_openapi.py` на успешных и ошибочных путях, включая 413. Полный production-трафик не измерялся. |
| NFR-13 | ✅ | OpenAPI 3.1 сохранён и валиден: 41 операция; `scripts/check_openapi.py` и контрактный pytest. |
| NFR-14 | ◐ | `/api/v1` применяется в `app/main.py`; UTC/ISO функции в `app/db.py`, `app/session.py`; `test_exam_binds_specific_scoring_rule_version` проверяет UTC для одного сценария. Сквозной контрактный тест JSON UTF-8 и дат для всех ответов отсутствует. |
| NFR-15 | ✅ | Тест логирования проверяет отсутствие чувствительного тела и неизвестного пути; аудит фильтрует контекст. Это проверка реализованных путей, не аттестация production. |
| NFR-16 | ✅ | Coverage всего `app` — 81,77%; Makefile и CI проверяют порог 80% по всему приложению. Отчёт `docs/coverage.xml`. |
| NFR-17 | ◐ | Пять миграций: upgrade/check/downgrade/upgrade и повторный seed прошли на SQLite. PostgreSQL недоступен локально; CI сценарий подготовлен. |
| NFR-18 | ◐ | Access state в JWT/SQL, ответы и idempotency в БД (`app/auth.py`, `app/session.py`); контракт не зависит от памяти процесса. Испытания с двумя экземплярами и общими зависимостями нет. |

## Критерии приёмки

| ID | Статус | Реализация и тест / недостающая проверка |
| --- | --- | --- |
| AC-01 | ✅ | `app/routers/auth.py`, `app/routers/admin.py`; `tests/test_auth.py::test_login_refresh_rotation_logout_and_profile`, `test_admin_validation_password_reset_and_live_role_revocation`. |
| AC-02 | ✅ | `app/models/content.py`, `app/routers/content.py`; `tests/test_content.py::test_published_snapshot_and_variant_order`. |
| AC-03 | ✅ | `app/session.py::start_session`, DB unique index; `tests/test_session.py::test_only_assigned_student_can_start_and_resume`, `test_database_allows_only_one_active_attempt_per_assignment`. |
| AC-04 | ✅ | `app/session.py::save_answer`; `tests/test_session.py::test_answer_revision_and_idempotency`. |
| AC-05 | ✅ | Проверены две независимые одноразовые ссылки, отказ при исчерпании лимита и настраиваемые лимиты 1/2/3; `tests/test_session.py`. |
| AC-06 | ✅ | Submit и самостоятельное завершение worker проверены в `tests/test_session.py`; повторное завершение не создаёт проверки заново. |
| AC-07 | ✅ | `app/scoring.py::process_submission`; `tests/test_scoring.py::test_submission_is_idempotent_and_recalculation_preserves_result_versions` проверяет сохранение raw, одну автопроверку и версию правила. |
| AC-08 | ✅ | `app/routers/review.py`; `tests/test_review_results.py::test_independent_review_dispute_access_and_versioned_result`, `test_claim_is_unique_until_lease_expires_and_old_owner_loses_access`. |
| AC-09 | ✅ | Независимая проверка, спор и решение по критериям проверены `tests/test_review_results.py`. |
| AC-10 | ✅ | `app/scoring.py::grade_for_score`; `tests/test_scoring.py::test_geometry_boundary_one_and_two` проверяет балл геометрии 1, оценку 2 и причину. |
| AC-11 | ✅ | `app/scoring.py::grade_for_score`, `app/models/review.py::ResultVersion`; `test_geometry_boundary_one_and_two`, `test_submission_is_idempotent_and_recalculation_preserves_result_versions`. |
| AC-12 | ✅ | `app/routers/results.py::recalculate_result`/`result_history`; `tests/test_review_results.py::test_independent_review_dispute_access_and_versioned_result`. |
| AC-13 | ◐ | IDOR результата и аудитора проверен `tests/test_review_results.py::test_protected_review_result_export_and_audit_routes_reject_wrong_roles`; отсутствие любых секретов во всех событиях аудита отдельным тестом/аудитом не подтверждено. |
| AC-14 | ✅ | `app/routers/results.py::export_results`; `tests/test_review_results.py::test_bi_export_exposes_only_anonymized_latest_result_data`. |
| AC-15 | ✅ | `app/routers/ops.py`; `tests/test_ops_openapi.py::test_health_separates_liveness_from_database_readiness`, `test_metrics_requires_service_token_and_returns_prometheus_text`. |
| AC-16 | ✅ | `app/main.py` exception handlers; `tests/test_ops_openapi.py::test_success_validation_auth_and_missing_route_share_correlation_contract`. |
| AC-17 | ○ | `load/k6.js` подготовлен, но нет измерений NFR-02–NFR-04 и fault test NFR-05. |
| AC-18 | ◐ | Локальные тесты, lint, mypy, OpenAPI и coverage gate прошли. Dependency audit обнаружил уязвимости; PostgreSQL/контейнеры/CI не запускались. |

## Дополнения мастер-промта и остаточные риски

- Импорт КИМ математики и физики: `app/kim.py`, `app/routers/kim.py`, `tests/test_kim.py`, `examples/kim/`; локальные схемы описаны в `docs/kim-import.md`. Официальные JSON Schema заказчика не предоставлены; эвристическая проверка персональных данных не заменяет модерацию.
- Подготовлены нагрузочный сценарий, backup/restore и offline demo. Измерения нагрузки и PostgreSQL restore ещё не выполнены.
- OpenAPI, миграции, CI, Docker, seed, Prometheus и backup-скрипт присутствуют в репозитории. Их наличие отделено выше от фактического выполнения на PostgreSQL, в контейнерах и под нагрузкой.
- Перед приёмкой требуется обновить уязвимые зависимости и провести PostgreSQL/контейнерные, нагрузочные, fault и restore испытания. Раздельные SQL rate limits для refresh/audio/answer/export реализованы в `app/rate_limit.py`; login имеет отдельную блокировку перебора.
