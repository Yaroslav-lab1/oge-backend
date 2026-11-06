# Контракт импорта КИМ

Импорт принимает сырые байты ZIP в `Content-Type: application/zip`. Доступ: `CONTENT_MANAGER` или `ADMIN`.

| Метод | Путь | Результат |
|---|---|---|
| POST | `/api/v1/kim-imports/validate` | Dry-run: `valid`, `checksum`, `issues`, без записи в БД |
| POST | `/api/v1/kim-imports` | Атомарный импорт, `id` операции и `packageVersionId` |
| GET | `/api/v1/kim-imports/{id}` | Статус и протокол операции |
| GET | `/api/v1/kim-packages` | Список пакетов, фильтр `subject` |
| GET | `/api/v1/kim-packages/{id}/versions` | Версии конкретного пакета |

## Локальная схема 1.0

Официальные JSON Schema не предоставлены. Ниже фиксируется локальный адаптер для `MATH-OGE-SCHEMA-1.0` и `PHYS-OGE-SCHEMA-1.0`; его проверка изолирована в `app/kim.py`.

Обязательны `passport.json`, `topics.json`, `plan.json`, `tree.json`, `tasks.json`, `prototypes.json`, `keys.json`, `criteria.json`, `instruction.md`, `materials.json`, `manifest.json`, каталог `resources/` и минимум один JSON в `variants/`.

- `passport.json`: объект с `subject` (`MATH` или `PHYS`), `name`, `schemaVersion`, `sourceVersion`.
- `topics.json`: массив объектов `{code, title, ...}`.
- `plan.json`, `tree.json`: JSON по предметному плану и дереву. Сохраняются побайтно; backend не меняет смысл этих документов.
- `tasks.json`: массив объектов `{code, topicCode, answerType, body, maxScore, geometry?, normalization?, audioResource?}`. `answerType`: `SINGLE_CHOICE`, `MULTIPLE_CHOICE`, `NUMBER`, `TEXT`, `ESSAY`.
- `prototypes.json`: массив объектов с обязательным `code`.
- `keys.json`: объект `{taskCode: {value, unit?}}`; для `ESSAY` ключ может отсутствовать.
- `criteria.json`: объект `{taskCode: {...}}`; для `ESSAY` критерии обязательны.
- `materials.json`: массив `{path: "resources/...", mediaType?}`.
- `variants/*.json`: объект `{code, taskCodes: [taskCode, ...]}`.
- `manifest.json`: `schemaVersion`, `sourceVersion`, `files`, где `files` отображает **каждый** файл, кроме самого manifest, в `{sha256, size}`. SHA-256 считается по исходным байтам файла.

Все файлы сохраняются побайтно в `KimPackageFile`, а предметные сущности дополнительно нормализуются. Импортированные `TaskVersion` и варианты сразу опубликованы и доступны как неизменяемые снимки. Новая версия пакета создаёт новые версии заданий, не изменяя старые. Номера версий математики и физики ведутся независимо. Повтор того же ZIP SHA-256 даёт статус `DUPLICATE` и существующий `packageVersionId`.

Неуспешный импорт возвращает `422 KIM_INVALID` с массивом `issues` и `jobId`. Протокол сохраняется в `KimImportJob`/`KimImportIssue`; содержимое ошибочного пакета в предметные таблицы не попадает.

## Ограничения и безопасность

Максимум 50 МиБ для ZIP и суммарного распакованного содержимого, 1000 записей. Запрещены абсолютные пути, `..`, обратная косая черта, симлинки, шифрованные записи, исполняемые/активные форматы, файлы вне объявленной структуры, внешние ссылки и очевидные персональные данные (email, телефон, трёхчастное ФИО). Проверяются контрольные суммы, размеры и связи тем, заданий, ключей, критериев, материалов и вариантов. Ошибка любого файла отклоняет импорт целиком. Проверка PII эвристическая; для эксплуатации с реальными материалами нужен отдельный утверждённый контентный контроль.

## Воспроизводимые синтетические пакеты

- `examples/kim/math-valid.zip` — математика, числовой ответ и геометрический флаг.
- `examples/kim/physics-valid.zip` — физика, числовой ответ с единицей и WAV.
- `examples/kim/invalid-checksum.zip` — неверный SHA-256 задания.
- `examples/kim/invalid-traversal.zip` — путь за пределами архива.

Файлы генерируются командой `python examples/kim/build_examples.py`. Все данные синтетические. Для проверки:

```bash
curl -X POST http://localhost:8000/api/v1/kim-imports/validate \
  -H 'Authorization: Bearer <access-token>' \
  -H 'Content-Type: application/zip' \
  --data-binary @examples/kim/math-valid.zip
```
