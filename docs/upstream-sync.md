# Sincronización del fork de Novigo con AgriTheory

Integración preparada el 26 de octubre de 2026 en la rama `update-main`.

- Base del fork: `bfe0f01` (`main`, versión 1.0.2).
- Original: [agritheory/test_utils](https://github.com/agritheory/test_utils), remoto `Test`, commit `8c31b62` (versión 1.30.1).
- Ancestro común: `92eb196`; se integran los 180 commits pendientes del original y se conserva la historia de los 31 commits propios mediante un merge.
- Respaldo: `backup/main-before-upstream-20261026`. `main` conserva su commit inicial.
- La rama `main-cherry` conserva su estado; esta integración parte de `main`.

## Personalizaciones conservadas

| Área | Decisión |
| --- | --- |
| Copyright | Inferir la aplicación desde `pyproject.toml` sin exigir `--app`, con soporte para metadatos PEP 621 y Poetry. Mantener ambas llamadas Python: `validate_copyright(files)` y `validate_copyright(app, files)`. |
| Interfaces del original | Admitir `--app`, diccionario con `app_publisher` y archivos SQL; conservar también el soporte local para HTML. Actualizar el filtro del manifiesto de hooks. |
| Escritura de archivos | Actualizar encabezados después del shebang y la declaración de codificación de Python. Conservar contenido, codificación, BOM UTF-8, saltos de línea y permisos; cerrar el temporal antes de moverlo y evitar escrituras repetidas si el encabezado ya coincide. |
| Python | Soportar exclusivamente Python 3.14 (`>=3.14,<3.15`), con herramientas, archivo `.python-version` y acciones que seleccionan Python 3.14. |
| Formato | Conservar Ruff y su configuración local, con versión 0.15.4 en desarrollo; incorporar el hook de duplicación del original. |
| Metadatos | Conservar descripción y autor de Novigo, lectura de versión desde `importlib.metadata` y actualizaciones locales de GitHub Actions. Adoptar el formato PEP 621, los nuevos comandos y dependencias y la versión 1.30.1 del original. |
| Distribución | Mantener `build/` y `dist/` ignorados y retirar los cuatro paquetes antiguos que el original incorpora al árbol. |
| Historial de versiones | Usar el changelog del original y conservar íntegro el changelog previo del fork en [changelog-novigo-before-sync.md](changelog-novigo-before-sync.md). |

## Ajustes de integración

Se recuperó `return invalid_accounts` en `find_invalid_account_links`: una limpieza local lo había eliminado, por lo que la función siempre devolvía `None`.

Los cambios locales que solo modificaban formato o imports en validadores y fixtures se reemplazaron por sus versiones del original para reducir diferencias en futuras sincronizaciones. El limpiador y el validador de customizaciones mantienen las nuevas implementaciones del original.

Se corrigieron tres entradas de comandos SQL: `sql_rewriter` apuntaba a un módulo inexistente; los hooks `sql_docstring_formatter` y `sql_registration` no tenían comandos instalables. También se corrigió `pytest.ini`, cuyo contenido original usaba sintaxis TOML en un archivo INI.

Se regeneró `poetry.lock` para el rango local de Python. Se actualizó NumPy dentro de sus restricciones existentes para usar una distribución binaria compatible con Python 3.14; el lock original seleccionaba NumPy 2.2.6 y obligaba a compilarlo desde las fuentes. La lectura de TOML del hook usa `tomllib`, incluido en Python, y ya no necesita la dependencia local `tomli`.

Había un secuenciador de cherry-pick antiguo, de marzo de 2026, cuyo estado ya no coincidía con `main`. Se respaldó en `/tmp/test-utils-before-sync-20261026/sequencer` y se retiró con `git cherry-pick --quit`, conservando los commits.

## Validación

- Suite completa con Python 3.14: 67 pruebas aprobadas, incluidas 52 para las interfaces del copyright, HTML/SQL, actualización de encabezados, shebang, codificación de Python, BOM UTF-8, saltos de línea, conservación del contenido y permisos e idempotencia.
- `poetry check --lock` y construcción de wheel y sdist.
- Instalación completa de dependencias de ejecución y desarrollo con el lock regenerado.
- Sintaxis Python, YAML, destinos de los 23 comandos y comandos usados por los hooks; ejecución de `--help` en 21 comandos instalados. Se excluyeron el comando de particiones que requiere Frappe y el actualizador de pre-commit, que modifica configuraciones.
- Validación de configuración y manifiesto de pre-commit.
- Ruff y formato aprobados para el hook modificado y sus pruebas.
- Ruff global: 170 observaciones heredadas; comparación por archivo y regla con `Test/main` sin observaciones nuevas. Una ejecución completa de pre-commit requerirá resolver también ese trabajo de estilo.

Las pruebas de impresoras abren sockets locales. La suite emite advertencias de obsolescencia de `pytest-asyncio` 0.24 con Python 3.14. No se ejecutaron operaciones de base de datos ni pruebas de integración en un bench de Frappe/ERPNext.

## Próximas sincronizaciones

El merge deja el commit original como ancestro de esta rama. Cuando esta integración esté en la rama de trabajo habitual:

```bash
git fetch Test
git merge --no-commit --no-ff Test/main
# Revisar conflictos y las personalizaciones descritas arriba.
poetry lock
poetry install --with dev
poetry run pytest -q
poetry check --lock
poetry build
git add -A
git commit
```

Conviene revisar `git diff Test/main` para identificar las personalizaciones que siguen pendientes de mantener. La publicación del fork y la integración en `main` se realizan por separado de esta preparación local.
