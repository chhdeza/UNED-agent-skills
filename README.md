# UNED Agent Skills

Skill de Claude Code para **calificar tareas de Moodle** (UNED Costa Rica): descarga las entregas, las califica en español contra la rúbrica aprobada, valida mecánicamente la escala y genera el resumen con casos para revisión del profesor. Opcionalmente sube las notas a Moodle.

> Proyecto comunitario no oficial, sin afiliación con Moodle, UNED ni Anthropic. La calificación asistida es un borrador: **la decisión final es siempre del profesor**.

## Instalación

1. Copie en la raíz de su workspace: `.claude/`, `prompt.txt`, `.env.example`, `Users.example.md`, `.gitignore` y `requirements.txt`.
2. `python -m pip install -r requirements.txt` (Python 3.10+).
3. `cp .env.example .env` y complete `MOODLE_URL`, `MOODLE_USERNAME`, `MOODLE_PASSWORD`.
4. Edite `prompt.txt` (nombre del curso y secciones obligatorias).
5. Abra Claude Code en esa carpeta y ejecute `/moodle-grading <course-id> <assignment-cmid>`.

## Contenido

| Ruta | Función |
|------|---------|
| `.claude/skills/moodle-grading/SKILL.md` | Flujo de 8 pasos (orquestador) |
| `.claude/skills/moodle-grading/grading-rules.md` | Reglas inmutables de calificación |
| `.claude/skills/moodle-grading/reference.md` | CLI de los scripts, formatos y solución de problemas |
| `.claude/skills/moodle-grading/log-instructions.md` | Cómo descargar los logs de Moodle |
| `.claude/skills/moodle-grading/scripts/` | Descarga, extracción de ZIP/usuarios, validación y resumen, subida |
| `.claude/agents/moodle-grader.md` | Subagente que califica una entrega (corre en paralelo) |

## Seguridad

`.env`, `submissions/`, `Users.md` y los logs contienen credenciales o datos de estudiantes y están excluidos por `.gitignore`. Las escrituras en sistemas oficiales (`--commit`) requieren autorización explícita en cada ejecución.

## Licencia

Apache License 2.0 (ver `LICENSE` y `NOTICE`).
