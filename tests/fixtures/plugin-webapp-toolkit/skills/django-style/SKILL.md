---
name: django-style
description: Use when the task repo is a Django project or the issue mentions Django, views, models, serializers, the ORM, migrations, or queryset behavior. Webapp fix conventions from the webapp-toolkit plugin.
---

# Django style (webapp-toolkit plugin)

- Fix the application code the failing test exercises; never edit the
  repo's test suite (the harness edit policy protects it anyway).
- Run the suite as `python -m pytest` from the repo root.
- Model changes need a migration: `python manage.py makemigrations
  <app>`; include the generated migration file in the fix.
- Prefer the ORM (`Model.objects.filter(...)`) over raw SQL.
- `select_related` for FK joins, `prefetch_related` for many-relations
  (N+1 is the top performance complaint class).
- After edits, a quick lint pass is available in read-only batches:
  `ruff check <file>` (this plugin registers the ruff verb).
