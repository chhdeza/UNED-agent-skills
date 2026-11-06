# Cómo descargar los logs (registros) de Moodle

Los logs del curso son la fuente para generar automáticamente la lista de estudiantes (`Users.md`) con `extract_users.py`. Siga estos 4 pasos.

## Paso 1 — Abrir los Informes del curso

Entre al curso en Moodle y, en el menú superior, haga clic en **Informes** → **Registros**.

En la pestaña **Informes**, seleccione **Registros**.

## Paso 2 — Configurar los filtros y pedir los registros

En la página de **Registros**:

1. Verifique que el primer dropdown tenga el **curso correcto**.
2. Si imparte un grupo específico, selecciónelo en el segundo dropdown (ej. «Grupo 02»); si no, déjelo en todos.
3. Deje **«Todos los participantes»**, **«Todos los días»** y el resto de filtros en sus valores por defecto (**Log estándar**).
4. Haga clic en **«Conseguir estos registros»**.

Use los filtros visibles en la página y pulse **Conseguir estos registros**.

## Paso 3 — Descargar como CSV

Baje hasta el final de la tabla de resultados. En **«Descargar datos de tabla como»** seleccione **«Valores separados por comas (.csv)»** y haga clic en **«Descargar»**.

Al pie de la tabla, seleccione el formato CSV y pulse **Descargar**.

## Paso 4 — Guardar el archivo en el workspace

Guarde el `.csv` en la **raíz del workspace** (la carpeta de este paquete), por ejemplo como `Logs Usuarios.csv` o `logs_00000_20260711.csv`.

Guarde el CSV en la raíz del workspace con un nombre que quede excluido por
`.gitignore`.

Luego genere la lista de estudiantes:

```bash
python .claude/skills/moodle-grading/scripts/extract_users.py "Logs Usuarios.csv" -o Users.md
```

## Formato esperado del CSV

El archivo descargado debe traer estas columnas (Moodle las genera automáticamente con el procedimiento anterior):

| Columna | Uso |
|---------|-----|
| `Nombre completo del usuario` | Nombre del estudiante |
| `Descripción` | Contiene `The user with id 'NNNN'`, de donde sale el User ID |
| `Contexto del evento` | Contiene `Curso: NOMBRE DEL CURSO` (detección automática del nombre) |

Ejemplo de una fila real:

```csv
Hora,"Nombre completo del usuario","Usuario afectado","Contexto del evento",Componente,"Nombre evento",Descripción,Origen,"Dirección IP"
"11/07/26, 09:14:41","NOMBRE APELLIDO EJEMPLO",-,"Curso: 00000 - CURSO DE EJEMPLO - IIC2026",Registros,"Informe de registro visualizado","The user with id '1001' viewed the log report for the course with id '2001'.",web,192.0.2.10
```

## Notas

- Si el curso tiene muchos eventos, puede descargar los logs en varias tandas (p. ej. filtrando por días); todos los CSV se pasan juntos a `extract_users.py`.
- Los logs solo incluyen usuarios **con actividad** en el período consultado. Si sospecha que falta algún estudiante, use «Todos los días» o agregue manualmente la fila a `Users.md`.
- Los CSV contienen datos de estudiantes: no los suba a Git (el `.gitignore` excluye `logs_*.csv`).
