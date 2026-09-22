---
name: django-conventions
description: Use when the task repo is a Django project or the issue mentions Django, models, migrations, views, serializers, the ORM, or queryset behavior. Django-specific fix conventions.
---

# Django conventions

- Model/schema changes require a migration: generate with
  `python manage.py makemigrations <app>` and include the generated
  migration file in the fix — an unaccompanied model edit breaks the
  next `migrate`.
- Query in the view's language: use the ORM (`Model.objects.filter
  (...)`) rather than raw SQL unless the issue explicitly shows SQL.
- Watch for N+1 shapes: `select_related` for FK/one-to-one,
  `prefetch_related` for many/many.
- Serializers: field mismatches surface as 500s/AttributeErrors at the
  API boundary; check the serializer's `fields` list matches the model
  change before assuming the view is wrong.
- Never edit the repo's test suite: tests/ is protected by the harness
  edit policy; fix the application code the test exercises.
