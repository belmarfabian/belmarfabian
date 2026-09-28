import csv

from pypdf import PdfReader, PdfWriter

import cep_pdr as c

# Texto de portada del PdR 766 tal como lo entrega la extracción: columnas entremezcladas.
PORTADA = """EDICIÓN CENTRO

DIGITAL DE ESTUDIOS

N° 766, ABRIL 2026 PÚBLICOS

puntos de referenciaPOLÍTICA Y DERECHO

De la reconciliación a la seguridad: la ampliación semántica de la democracia en los discursos inaugurales de Chile (1990-2026)

FABIÁN BELMAR, ALDO MASCAREÑO, JUAN ROZAS Y ANDRÉS ARAYA

N° 766, ABRIL 2026 POLÍTICA Y DERECHO PUNTOS DE REFERENCIA

RESUMEN

Este artículo analiza los nueve discursos inaugurales.

Palabras clave: elecciones discurso inaugural, análisis de texto computacional, RILE, democracia

FABIÁN BELMAR es investigador asistente del Centro de Estudios Públicos.

ALDO MASCAREÑO es investigador senior del Centro de Estudios Públicos.

JUAN ROZAS es investigador asistente del Centro de Estudios Públicos.

ANDRÉS ARAYA es investigador asistente del Centro de Estudios Públicos.
"""


def test_extraer_portada():
    d = c.extraer(PORTADA)
    assert (d["numero"], d["anio"], d["mes"]) == ("766", "2026", "4")
    assert d["area"] == "Política y Derecho"
    assert d["titulo"].startswith("De la reconciliación a la seguridad")
    assert d["titulo"].endswith("(1990-2026)")
    assert d["autores"] == "Fabián Belmar; Aldo Mascareño; Juan Rozas; Andrés Araya"
    assert d["palabras_clave"].split(", ")[-1] == "democracia"
    assert c.nuevo_nombre(d) == "2026_PdR766_Belmar-Mascareno-etal.pdf"


def test_autores_desde_linea_de_portada_sin_bios():
    d = c.extraer(PORTADA.split("FABIÁN BELMAR es")[0])
    assert d["autores"] == "Fabián Belmar; Aldo Mascareño; Juan Rozas; Andrés Araya"


def test_nombre_archivo_uno_y_dos_autores():
    base = {"anio": "2024", "numero": "42"}
    assert c.nuevo_nombre({**base, "autores": "Aldo Mascareño"}) == "2024_PdR042_Mascareno.pdf"
    assert c.nuevo_nombre({**base, "autores": "Fabián Belmar; Juan Rozas"}) == "2024_PdR042_Belmar-Rozas.pdf"


def test_apply_escribe_info_y_xmp(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    w = PdfWriter()
    w.add_blank_page(612, 792)
    w.add_metadata({"/Title": "PdR766.indd"})
    with (raw / "x.pdf").open("wb") as f:
        w.write(f)

    d = c.extraer(PORTADA)
    fila = {"archivo": "x.pdf", **d, "nuevo_nombre": c.nuevo_nombre(d), "estado": "ok", "pdf_url": ""}
    with (tmp_path / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=c.MANIFEST_FIELDS)
        wr.writeheader()
        wr.writerow(fila)

    c.main(["--data", str(tmp_path), "apply"])
    r = PdfReader(tmp_path / "final" / fila["nuevo_nombre"])
    assert r.metadata["/Title"] == d["titulo"]
    assert r.metadata["/CreationDate"] == "D:20260401000000"
    assert r.xmp_metadata.dc_title["x-default"] == d["titulo"]
    assert r.xmp_metadata.dc_creator == ["Fabián Belmar", "Aldo Mascareño", "Juan Rozas", "Andrés Araya"]
    assert (raw / "x.pdf").exists()  # el original no se toca


def test_area_pegada_al_encabezado():
    d = c.extraer("N° 650, MARZO 2023POLÍTICA Y DERECHOPUNTOS DE REFERENCIA\nRESUMEN\n")
    assert (d["numero"], d["area"]) == ("650", "Política y Derecho")


def test_area_en_linea_de_portada():
    d = c.extraer("EDICIÓN DIGITAL\nN° 650, MARZO 2023\npuntos de referencia POLÍTICA Y DERECHO\n")
    assert d["area"] == "Política y Derecho"


def test_apellidos_con_iniciales_y_particulas():
    assert c.apellido("Rodrigo Vergara M.") == "Vergara"
    assert c.apellido("Rosario Palacios R. de G.") == "Palacios"
    assert c.apellido("Tomás de la Maza B.") == "DeLaMaza"
