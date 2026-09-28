# Puntos de Referencia del CEP: descarga y metadatos

`cep_pdr.py` descarga los PDF de la serie *Puntos de Referencia* del Centro de Estudios Públicos y corrige sus metadatos. Los campos de cada archivo quedan con título, autores, número, fecha, área y palabras clave, y el nombre del archivo sigue un patrón uniforme:

```
2026_PdR766_Belmar-Mascareno-etal.pdf
```

Los metadatos se escriben dos veces: en el diccionario `Info` del PDF y en el paquete XMP. Hay que hacerlo en ambos lugares porque Acrobat, Zotero y la mayoría de los lectores dan prioridad al XMP, y el que deja InDesign suele traer el nombre del archivo `.indd` como título.

## Uso

```bash
pip install -r requirements.txt

python cep_pdr.py all       # rastreo + descarga + extracción -> data/manifest.csv
# revisar data/manifest.csv (se puede editar a mano)
python cep_pdr.py apply     # escribe metadatos y copia renombrada en data/final/
```

Cada paso también se puede correr por separado (`crawl`, `download`, `extract`, `apply`) y todos se pueden retomar: la descarga salta los archivos que ya están en `data/raw/`. Los originales nunca se modifican.

## Qué hace cada paso

| Paso | Salida | Detalle |
|---|---|---|
| `crawl` | `data/urls.csv` | Lee los sitemaps de `cepchile.cl` y recorre el sitio respetando `robots.txt`, con un segundo entre solicitudes. Marca con pista los PDF cuyo enlace, texto o página mencionan *Puntos de Referencia*. |
| `download` | `data/raw/` | Descarga solo los PDF con pista. Con `--all-pdfs` descarga todos. |
| `extract` | `data/manifest.csv` | Lee las primeras páginas: encabezado `N° 766, ABRIL 2026 ÁREA PUNTOS DE REFERENCIA`, título, línea de autores y notas biográficas (`NOMBRE es investigador...`), y *Palabras clave*. |
| `apply` | `data/final/` | Escribe `Info` y XMP (Dublin Core y PRISM) y guarda la copia con el nombre nuevo. |

La columna `estado` del manifiesto clasifica cada archivo:

- `ok`: se aplica.
- `revisar: falta …`: la extracción no encontró algún campo. Se completa a mano y se cambia a `ok`, o se aplica igual con `apply --include-review`.
- `no_pdr`: el PDF no es de la serie, así que se omite.

## Límites conocidos

- Fue probado contra el texto de la portada del PdR 766 y contra un sitio simulado, no contra `cepchile.cl`, que estaba bloqueado en el entorno donde se escribió el script. En la primera corrida conviene revisar el resumen de `crawl`. Si el rastreo completo resulta lento, se puede acotar con `--seed <URL del listado de la serie>` y `--follow <regex>`.
- El apellido que va en el nombre del archivo es la última palabra del nombre, junto con sus partículas (*de la*, *van*). Con apellidos compuestos esto puede fallar, y en ese caso se corrige la columna `nuevo_nombre` del manifiesto.
- Los PdR antiguos escaneados no tienen texto extraíble y quedan como `revisar`.
- `/CreationDate` se fija en el primer día del mes de publicación.

## Pruebas

```bash
pip install pytest && python -m pytest -q
```
