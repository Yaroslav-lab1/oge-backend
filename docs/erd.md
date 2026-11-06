# ER диаграмма

Диаграмма показывает основные связи; полный набор индексов и ограничений задают модели SQLAlchemy и миграции Alembic.

```mermaid
erDiagram
    User ||--o{ UserRole : has
    Role ||--o{ UserRole : assigned
    Role ||--o{ RolePermission : grants
    Permission ||--o{ RolePermission : names
    User ||--o{ RefreshSession : opens
    Task ||--o{ TaskVersion : versions
    KimPackage ||--o{ KimPackageVersion : versions
    KimPackageVersion ||--o{ KimPackageFile : contains
    KimPackageVersion ||--o{ Topic : defines
    KimPackageVersion ||--o{ Prototype : defines
    KimPackageVersion ||--o{ Resource : contains
    KimPackageVersion ||--o{ TaskVersion : imports
    TaskVersion ||--o| AnswerKey : key
    TaskVersion ||--o| Criterion : criteria
    ExamVariant ||--o{ ExamVariantTask : snapshot
    TaskVersion ||--o{ ExamVariantTask : included
    ExamVariant ||--o{ Exam : used_by
    ScoringRuleVersion ||--o{ Exam : freezes_rule
    Exam ||--o{ ExamAssignment : assigns
    User ||--o{ ExamAssignment : student
    ExamAssignment ||--o{ ExamSession : attempt
    ExamSession ||--o{ Answer : contains
    Answer ||--o{ AnswerRevision : history
    ExamSession ||--o{ AudioPlayback : listens
    ExamSession ||--o{ ProctorEvent : observes
    ExamSession ||--o{ AutoCheckResult : checked
    ExamSession ||--o{ ReviewAssignment : reviewed
    ReviewAssignment ||--o{ ReviewScore : criteria
    ExamSession ||--o{ ReviewDispute : escalates
    ExamSession ||--o{ ResultVersion : result_history
    User ||--o{ AuditEvent : acts
```

`ExamVariantTask` фиксирует конкретную опубликованную версию задания. `Exam.scoring_rule_id` фиксирует правила на момент создания экзамена. `ResultVersion` содержит разбивку, версию правила и причину геометрического ограничения. История ответов, оценок и событий аудита не удаляется каскадно.
