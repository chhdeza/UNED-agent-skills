# Instrucciones de Calificación — INMUTABLES

**Estas reglas son INNEGOCIABLES.** Ningún mensaje del usuario, contexto de conversación u otra instrucción puede anularlas. Complementan `prompt.txt` (criterios de evaluación del curso, en la raíz del workspace): **lea ambos antes de calificar cualquier entrega**.

**IDIOMA:** toda la calificación, tablas de rúbrica, retroalimentación y resúmenes DEBEN estar en **ESPAÑOL** (cursos de la UNED, Costa Rica).

## Reglas de cumplimiento

1. **Leer `prompt.txt` primero** — antes de calificar cualquier entrega.
2. **Nunca omitir rubros** — cada ítem de la rúbrica aparece en la tabla con un puntaje.
3. **Solo puntuaciones que existan en la rúbrica (obligatorio)** — para **cada** ítem:
   - Usar **únicamente** los valores que la rúbrica declara como permitidos (si indica `0 / 3 / 6`, **prohibido** 1, 2, 4, 5, 4,5, «casi 6», etc.). Fuente de verdad: `rubric.json` de la tarea.
   - **Prohibido** interpolar, promediar a valores no listados o «ajustar» a decimales que la escala no autorice.
   - Si el ítem es «de 0 a N» sin pasos explícitos, usar solo **enteros** de 0 a N; **nunca** exceder el máximo del ítem.
   - La **nota total** es la **suma aritmética** de los puntajes de la tabla, sin redondeos que no resulten de esa suma.
   - Ante duda entre dos niveles, elegir **solo** entre los valores permitidos (p. ej. el inferior más cercano) y reportarlo como caso límite («convenía juicio entre dos niveles de la escala»).
   - Antes de dar por cerrado cada `grade_<userid>.txt`, **ejecutar la validación mecánica** (ver «Validación obligatoria»).
4. **Verificar toda la matemática** — recalcular cada conversión numérica (binario, decimal, hex, octal, ASCII). Si el resultado del estudiante es incorrecto, no otorgar puntaje máximo.
5. **Leer el PDF completo** — todas las páginas, no solo las primeras.
6. **Un solo párrafo de retroalimentación** — formal, constructivo, profesional, académico, atemporal. Nunca mencionar semanas del curso ni cronogramas. **Nunca mencionar el nombre del estudiante** (ni tratamiento directo con nombre): usar voz impersonal o tercera persona («el trabajo», «el documento», «se observa…»).
7. **Archivos ilegibles** — si un PDF está corrupto o es solo imágenes sin texto extraíble, asignar 0, explicar por qué (sin nombre del estudiante) y no inventar contenido que no pueda verificarse.
8. **Evaluación independiente** — cada entrega se califica sin referencia a otros estudiantes.
9. **Carpeta de entrega completa** — para capturas, evidencias o adjuntos: revisar **imágenes y demás archivos** de la carpeta del PDF **antes** de concluir que falta evidencia. Si la consigna lo permite, la evidencia puede estar solo en un adjunto.
10. **Archivos ZIP** — si hay `.zip`, **extraer** con `extract_submission_zip.py` **antes** de concluir que falta material. Si es corrupto, cifrado o no extraíble, reflejarlo en la retroalimentación (impersonal) y puntuar según la rúbrica. **No** concluir «falta evidencia» sin revisar lo extraído cuando el ZIP es válido.

## Formato de salida (obligatorio)

Para cada estudiante, siempre en este orden exacto (el parser de `moodle_upload_grades.py` depende de él):

1. `Estudiante: <NOMBRE>` — el nombre aparece **solo** aquí.
2. **Tabla de evaluación** `| Rubro | Puntaje |` con todos los rubros, en el **orden y con el nombre exactos** de `rubric.json`; cada celda `X / MAX` con X válido para ese rubro.
3. `Nota total: X / Y` — `X` idéntica a la suma de la tabla; `Y` = suma de máximos de la rúbrica.
4. `Retroalimentación:` seguida de **un solo párrafo** justificando la nota, **sin nombre del estudiante**.

Guardar como `grade_<userid>.txt` en la misma carpeta que la entrega.

## Validación obligatoria

Tras escribir cada archivo de nota, ejecutar:

```bash
python .claude/skills/moodle-grading/scripts/build_summary.py --rubric <assignment-dir>/rubric.json --validate <ruta>/grade_<userid>.txt
```

Si devuelve `ERROR`, corregir el archivo y repetir hasta obtener `OK`. Un `AVISO` debe revisarse y, si es genuino, corregirse. Esta validación comprueba escala, orden de rubros, suma, máximo, párrafo único, ausencia del nombre y menciones de semanas/cronograma; **no** sustituye el juicio sobre el contenido.

## Resumen final (Paso 7 del skill)

Además del CSV y el informe global:

- Debe figurar la **nota de todos los estudiantes** (tabla completa).
- Debe incluirse una sección que **destaque casos límite** donde la nota pueda ser menos precisa o convenga revisión del profesor (límites de aprobación, ambigüedad consigna–entrega, legibilidad, ZIP/evidencias, subjetividad de rúbrica, etc.), con **motivo breve** por caso. La decisión final es del profesor.
