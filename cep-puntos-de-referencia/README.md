# Puntos de Referencia del CEP: descarga y metadatos

`cep_pdr.py` descarga todos los PDF de la serie *Puntos de Referencia* del Centro de Estudios Públicos y corrige sus metadatos. Cada archivo queda con título, autores, número, fecha, área y palabras clave, y con un nombre uniforme:

```
2026_PdR766_Belmar-Mascareno-etal.pdf
```

## Uso

```bash
pip install -r requirements.txt

python cep_pdr.py all       # catálogo + descarga + cruce -> data/manifest.csv
# revisar data/manifest.csv (se puede editar a mano)
python cep_pdr.py apply     # escribe metadatos y copia renombrada en data/final/
```

Cada paso también se puede correr por separado (`catalog`, `download`, `extract`, `apply`). La descarga se puede retomar, porque salta los archivos que ya están en `data/raw/`. Los originales nunca se modifican.

## De dónde sale cada dato

El sitio del CEP es un WordPress con API pública (`/wp-json/wp/v2/investigation`), y cada publicación trae una ficha con campos `citation_*`. La serie corresponde a `acf.categoria == 6`.

| Campo | Fuente principal | Respaldo |
|---|---|---|
| Número | «N° 766, abril 2026» en la ficha | encabezado del PDF |
| Fecha | mes y año de ese mismo texto | `citation_publication_date` |
| Título | `citation_title` | portada del PDF |
| Autores | `citation_authors` | fichas del equipo (`/team/{id}`) o portada del PDF |
| Área | encabezado del PDF («POLÍTICA Y DERECHO») | — |
| Palabras clave | «Palabras clave:» en el PDF | — |
| PDF | `citation_pdf_url` | adjunto `archivo`, o enlace en la página |

La fecha se toma del texto del número y no de la fecha de la ficha porque los números antiguos figuran en el sitio con la fecha en que se cargaron (2001), no con la de publicación.

Los metadatos se escriben dos veces: en el diccionario `Info` del PDF y en el paquete XMP (Dublin Core y PRISM). Acrobat, Zotero y la mayoría de los lectores dan prioridad al XMP, y el que deja InDesign suele traer como título el nombre del archivo `.indd`.

### Errores del sitio que el script corrige

- **Campos `citation_*` copiados de otra ficha.** Las fichas 40942 (N° 785) y 40819 (N° 783) conservaban título, autores y PDF de otro número. Se detecta porque el número o el título de la ficha no coinciden con los `citation_*`; en ese caso se usan los campos propios, los autores de `acf.autores` y el PDF enlazado en la página.
- **Dos fichas con el mismo PDF.** Se conserva la ficha cuyo número coincide con el impreso en el PDF; la otra queda como `duplicado`.
- **Publicaciones mal catalogadas** (un Documento de Trabajo y una Encuesta CEP) y autores ausentes en la ficha: se resuelven en `correcciones.csv`, con la fuente de cada decisión. `extract` aplica ese archivo automáticamente.

## Estados en `manifest.csv`

- `ok`: se aplica.
- `revisar: …`: falta algún campo, o el número del PDF no coincide con el de la ficha. Se corrige la fila y se cambia a `ok`, o se aplica igual con `apply --include-review`.
- `sin_pdf`: la ficha existe, pero el sitio no publica el archivo (por ejemplo, los N° 1 a 12, de 1986-87).
- `duplicado` / `excluido`: no se aplican; el motivo va en la misma columna o en `correcciones.csv`.

## Resultado (28 de septiembre de 2026)

796 fichas, 769 PDF únicos con metadatos corregidos (N° 13 a 785). Faltan en el sitio los N° 1-12, 105, 176, 311 y 415: sus fichas no enlazan archivo o no existen.

## Límites conocidos

- El apellido que va en el nombre del archivo es la última palabra del nombre, sin iniciales y junto con sus partículas (*de la*, *le*). Con apellidos compuestos, como «Mora y Araujo», puede fallar; en ese caso se corrige `nuevo_nombre`.
- En los PDF escaneados no hay texto extraíble, así que quedan sin área ni palabras clave.
- `/CreationDate` se fija en el primer día del mes de publicación.

## Pruebas

```bash
pip install pytest && python -m pytest -q
```
