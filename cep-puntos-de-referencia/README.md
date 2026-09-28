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

## Estados en `manifest.csv`

- `ok`: se aplica.
- `revisar: …`: falta algún campo, o el número del PDF no coincide con el de la ficha. Se corrige la fila y se cambia a `ok`, o se aplica igual con `apply --include-review`.
- `sin_pdf`: la ficha existe, pero el sitio no publica el archivo (por ejemplo, los N° 1 a 12, de 1986-87).

## Límites conocidos

- El apellido que va en el nombre del archivo es la última palabra del nombre, sin iniciales y junto con sus partículas (*de la*, *le*). Con apellidos compuestos, como «Mora y Araujo», puede fallar; en ese caso se corrige `nuevo_nombre`.
- En los PDF escaneados no hay texto extraíble, así que quedan sin área ni palabras clave.
- `/CreationDate` se fija en el primer día del mes de publicación.

## Pruebas

```bash
pip install pytest && python -m pytest -q
```
