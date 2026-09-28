#!/usr/bin/env python3
"""Descarga la serie Puntos de Referencia del CEP y corrige los metadatos de cada PDF.

Flujo en cuatro pasos, cada uno retomable:

  catalog   lee la ficha de cada PdR desde la API de cepchile.cl  -> data/catalogo.csv
  download  descarga los PDF                                       -> data/raw/
  extract   cruza la ficha con la portada del PDF                  -> data/manifest.csv
  apply     escribe metadatos (Info + XMP) y copia renombrada      -> data/final/

La ficha web (título, autores, fecha, número) manda; del PDF se toman el área y
las palabras clave, y se completan los campos que la ficha no trae. El manifiesto
es editable: si algo queda mal, se corrige la fila y se vuelve a correr `apply`.
Los originales no se tocan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

API = "https://www.cepchile.cl/wp-json/wp/v2/"
CATEGORIA_PDR = "6"  # acf.categoria de «Puntos de Referencia» en el tipo «investigation»
UA = "Mozilla/5.0 (compatible; cep-pdr-archiver/1.0; investigacion academica)"

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
PARTICULAS = {"de", "del", "la", "las", "los", "le", "y", "e", "van", "von", "da", "di"}
MAYUS = "A-ZÁÉÍÓÚÑÜ"

CATALOGO_FIELDS = ["id", "numero", "anio", "mes", "titulo", "autores", "pdf_url", "page_url"]
MANIFEST_FIELDS = [
    "id", "archivo", "numero", "anio", "mes", "area", "titulo", "autores",
    "palabras_clave", "nuevo_nombre", "estado", "pdf_url", "page_url",
]


# --------------------------------------------------------------------------- red

def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def get(s: requests.Session, url: str, delay: float, **kw) -> requests.Response | None:
    for intento in range(4):
        try:
            r = s.get(url, timeout=120, **kw)
            time.sleep(delay)
            if r.status_code == 200:
                return r
            if r.status_code in (400, 404, 410):
                return None
        except requests.RequestException as e:
            print(f"  error de red ({e.__class__.__name__}) en {url}", file=sys.stderr)
        time.sleep(2 ** (intento + 1))
    return None


# ------------------------------------------------------------------- catálogo

def limpiar_html(t: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()


def fecha_de(acf: dict, fecha_post: str) -> tuple[str, str]:
    # «N° 1, mayo 1986» manda: en los números antiguos la fecha de la ficha es la de
    # su carga al sitio (2001), no la de publicación.
    for campo in ("numero", "citation_technical_report_number"):
        m = re.search(r"([a-záéíóú]+)\s+(?:de\s+)?(\d{4})", str(acf.get(campo) or ""), re.I)
        if m and m.group(1).lower() in MESES:
            return m.group(2), str(MESES[m.group(1).lower()])
    m = re.match(r"(\d{4})[/-]?(\d{2})", str(acf.get("citation_publication_date") or ""))
    if m:
        return m.group(1), str(int(m.group(2)))
    if fecha_post:
        return fecha_post[:4], str(int(fecha_post[5:7]))
    return "", ""


def numero_de(acf: dict) -> str:
    for campo in ("citation_technical_report_number", "numero"):
        m = re.search(r"N\s*[°ºo.]*\s*(\d{1,4})", str(acf.get(campo) or ""))
        if m:
            return m.group(1)
    return ""


def citation_vigente(it: dict) -> bool:
    """Falso si los campos citation_* quedaron copiados de otra ficha.

    Pasa en fichas nuevas: la 40942 (N° 785) conserva título, autores y PDF del N° 784.
    Se detecta porque su número o su título no coinciden con los propios de la ficha.
    """
    acf = it["acf"]
    n_ficha = numero_de({"numero": acf.get("numero")})
    n_cita = numero_de({"numero": acf.get("citation_technical_report_number")})
    if n_ficha and n_cita and n_ficha != n_cita:
        return False
    t_ficha = re.sub(r"\W+", "", limpiar_html(it["title"]["rendered"]).lower())[:25]
    t_cita = re.sub(r"\W+", "", limpiar_html(acf.get("citation_title") or "").lower())[:25]
    return not (t_ficha and t_cita and t_ficha != t_cita)


def pdf_de(s: requests.Session, acf: dict, page_url: str, delay: float) -> str:
    if acf.get("citation_pdf_url"):
        return acf["citation_pdf_url"]
    archivo = acf.get("archivo")
    if isinstance(archivo, dict) and archivo.get("url"):
        return archivo["url"]
    if isinstance(archivo, int) and archivo:
        r = get(s, f"{API}media/{archivo}?_fields=source_url", delay)
        if r and r.json().get("source_url", "").lower().endswith(".pdf"):
            return r.json()["source_url"]
    enlaces = re.findall(r"https?://[^\"'\\\s]+?\.pdf", json.dumps(acf))
    if enlaces:
        return enlaces[0]
    r = get(s, page_url, delay)  # último recurso: el enlace de descarga en la página
    if r:
        enlaces = re.findall(r"https?://static\.cepchile\.cl/[^\"'\s]+?\.pdf", r.text)
        if enlaces:
            return enlaces[0]
    return ""


def autores_de(s: requests.Session, acf: dict, delay: float, cache: dict) -> str:
    nombres = [a.get("citation_author", "").strip() for a in (acf.get("citation_authors") or [])]
    nombres = [n for n in nombres if n]
    if not nombres:
        for i in acf.get("autores") or []:
            if i not in cache:
                r = get(s, f"{API}team/{i}?_fields=title", delay)
                cache[i] = limpiar_html(r.json()["title"]["rendered"]) if r else ""
            if cache[i]:
                nombres.append(cache[i])
    return "; ".join(nombres)


def catalog(args) -> None:
    s = session()
    filas, pagina, total = [], 1, None
    while total is None or pagina <= total:
        r = get(s, f"{API}investigation?per_page=100&page={pagina}&_fields=id,date,link,title,acf",
                args.delay)
        if r is None:
            sys.exit(f"No se pudo leer la página {pagina} de la API.")
        total = int(r.headers.get("X-WP-TotalPages", 1))
        for it in r.json():
            acf = it.get("acf") or {}
            if str(acf.get("categoria")) == CATEGORIA_PDR:
                filas.append(it)
        print(f"  API {pagina}/{total}: {len(filas)} PdR")
        pagina += 1

    cache: dict = {}
    salida = []
    for it in filas:
        acf = it["acf"]
        if not citation_vigente(it):
            print(f"  ficha {it['id']}: campos citation_* copiados de otra; se usan los propios")
            acf = {k: v for k, v in acf.items() if not k.startswith("citation_")}
        anio, mes = fecha_de(acf, it.get("date", ""))
        salida.append({
            "id": it["id"],
            "numero": numero_de(acf),
            "anio": anio,
            "mes": mes,
            "titulo": limpiar_html(acf.get("citation_title") or it["title"]["rendered"]),
            "autores": autores_de(s, acf, args.delay, cache),
            "pdf_url": pdf_de(s, acf, it["link"], args.delay),
            "page_url": it["link"],
        })
    salida.sort(key=lambda f: (int(f["numero"]) if f["numero"] else 10**6, f["anio"]))
    destino = Path(args.data) / "catalogo.csv"
    destino.parent.mkdir(parents=True, exist_ok=True)
    escribir_csv(destino, CATALOGO_FIELDS, salida)
    con_pdf = sum(1 for f in salida if f["pdf_url"])
    print(f"{len(salida)} PdR en el catálogo, {con_pdf} con PDF -> {destino}")


def escribir_csv(ruta: Path, campos: list[str], filas: list[dict]) -> None:
    with ruta.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)


def leer_csv(ruta: Path) -> list[dict]:
    with ruta.open(encoding="utf-8") as f:
        return [{k: (v or "") for k, v in fila.items()} for fila in csv.DictReader(f)]


def download(args) -> None:
    data = Path(args.data)
    raw = data / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    filas = [f for f in leer_csv(data / "catalogo.csv") if f["pdf_url"]]
    s = session()
    fallos = 0
    for i, fila in enumerate(filas, 1):
        destino = raw / f"{fila['id']}.pdf"
        if destino.exists() and destino.stat().st_size > 0:
            continue
        r = get(s, fila["pdf_url"], args.delay)
        if r is None or not r.content.lstrip().startswith(b"%PDF"):
            fallos += 1
            print(f"  [{i}/{len(filas)}] falló {fila['pdf_url']}", file=sys.stderr)
            continue
        destino.write_bytes(r.content)
        if i % 25 == 0:
            print(f"  [{i}/{len(filas)}] descargados")
    print(f"{len(list(raw.glob('*.pdf')))} PDF en {raw}; {fallos} fallos")


# --------------------------------------------------------------- extracción

def texto_portada(reader: PdfReader, paginas: int = 3) -> str:
    partes = []
    for p in reader.pages[:paginas]:
        try:
            partes.append(p.extract_text() or "")
        except Exception:
            partes.append("")
    return "\n".join(partes)


def normalizar(t: str) -> str:
    t = unicodedata.normalize("NFC", t).replace("­", "")
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)  # guiones de corte de línea
    return re.sub(r"[ \t]+", " ", t)


def capitalizar(nombre: str) -> str:
    palabras = nombre.strip().lower().split()
    return " ".join(p if (p in PARTICULAS and i) else p[:1].upper() + p[1:]
                    for i, p in enumerate(palabras))


def separar_autores(linea: str) -> list[str]:
    partes = re.split(r",\s*|\s+[YE]\s+", linea.strip())
    return [capitalizar(p) for p in partes if p.strip()]


def extraer(texto: str) -> dict:
    """Metadatos legibles en la portada de un PdR (texto extraído del PDF)."""
    t = normalizar(texto)
    d = {"numero": "", "anio": "", "mes": "", "area": "", "titulo": "", "autores": "", "palabras_clave": ""}

    # El encabezado se repite en cada página («N° 766, ABRIL 2026 ÁREA PUNTOS DE REFERENCIA»);
    # en la portada suele venir entremezclado con otras columnas, así que se revisan todos.
    # Solo cuenta el número que está junto a las marcas de la serie; el cuerpo cita
    # «DFL N° 4 de 1959» o «Documento de Trabajo N° 94» y esos no son el número del PdR.
    marca = re.compile(r"puntos\s+de\s+refe|cepchile|estudios\s+p[uú]blicos|edici[oó]n\s+digital", re.I)
    encabezados = []
    # Los números de 1987-1993 dicen «Número 106 Noviembre 1992», siempre en la portada.
    for m in re.finditer(rf"(N[°º o.]*|N[úu]mero)\s*(\d{{1,4}}),?\s+([{MAYUS}a-záéíóúñ]+)\s+(\d{{4}})", t):
        if m.group(1)[:2] != "Nú" and m.group(1)[:2] != "Nu" and \
                not marca.search(t[max(0, m.start() - 60):m.end() + 80]):
            continue
        if m.group(3).lower() not in MESES:
            continue
        encabezados.append((m.group(2), m.group(4), str(MESES[m.group(3).lower()])))
        # A veces sin espacios: «N° 650, MARZO 2023POLÍTICA Y DERECHOPUNTOS DE REFERENCIA».
        a = re.match(rf"\s*([{MAYUS}][{MAYUS} ,]*?[{MAYUS}])\s*PUNTOS DE REFERENCIA", t[m.end():m.end() + 120])
        if a and not d["area"]:
            d["area"] = capitalizar(a.group(1))
    if encabezados:
        # El encabezado se repite página a página; una cita a otro PdR aparece una vez.
        d["numero"], d["anio"], d["mes"] = Counter(encabezados).most_common(1)[0][0]
    if not d["area"]:  # «puntos de referencia POLÍTICA Y DERECHO» en la portada
        a = re.search(rf"puntos de referencia[ \t]+([{MAYUS}][{MAYUS} ,]*[{MAYUS}])[ \t]*$", t, re.M)
        if a:
            d["area"] = capitalizar(a.group(1))

    # Notas biográficas: «NOMBRE APELLIDO es investigador...».
    bios = re.findall(rf"(?m)^\s*([{MAYUS}][{MAYUS}'.\- ]{{3,}}?)\s+(?:es|fue)\s+[a-záéíóú]", t)
    autores = [capitalizar(b) for b in dict.fromkeys(b.strip() for b in bios)]

    # Línea de autores de portada: mayúsculas, con coma o «Y».
    linea_autores, pos_autores = "", -1
    for mm in re.finditer(rf"(?m)^\s*([{MAYUS}][{MAYUS}'.\-]+(?:[ ,]+[{MAYUS}][{MAYUS}'.\-]*)+)\s*$", t):
        cand = mm.group(1)
        if re.search(r"PUNTOS DE REFERENCIA|RESUMEN|CENTRO DE ESTUDIOS|EDICI[OÓ]N|P[UÚ]BLICOS", cand):
            continue
        if autores and not any(a.split()[-1].upper() in cand for a in autores):
            continue
        if "," in cand or re.search(r"\s[YE]\s", cand) or autores:
            linea_autores, pos_autores = cand, mm.start()
            break
    if not autores and linea_autores:
        autores = separar_autores(linea_autores)
    d["autores"] = "; ".join(autores)

    if pos_autores > 0:
        previas = [l.strip() for l in t[:pos_autores].splitlines() if l.strip()]
        bloque = []
        for l in reversed(previas):
            if re.search(r"PUNTOS DE REFERENCIA|puntos de referencia|N°\s*\d|^[{0}\s,]+$".format(MAYUS), l):
                break
            bloque.insert(0, l)
            if len(bloque) >= 4:
                break
        d["titulo"] = re.sub(r"\s+", " ", " ".join(bloque)).strip(" .")

    k = re.search(r"Palabras\s+clave\s*:\s*(.+?)(?:\n\s*\n|\n[{0}]{{3,}}|$)".format(MAYUS), t, re.S)
    if k:
        claves = [re.sub(r"\s+", " ", c).strip(" .") for c in re.split(r"[,;]", k.group(1))]
        d["palabras_clave"] = ", ".join(c for c in claves if c)
    return d


def ascii_slug(t: str) -> str:
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-")


def apellido(nombre: str) -> str:
    # «Rodrigo Vergara M.» -> Vergara; «Rosario Palacios R. de G.» -> Palacios;
    # «Tomás de la Maza B.» -> DeLaMaza.
    partes = [p for p in nombre.split() if not re.fullmatch(r"[A-ZÁÉÍÓÚÑ]\.", p)]
    while len(partes) > 1 and partes[-1].lower() in PARTICULAS:
        partes.pop()
    if not partes:
        return ""
    ap = [partes[-1]]
    for p in reversed(partes[1:-1]):
        if p.lower() in PARTICULAS - {"y", "e"}:
            ap.insert(0, p)
        else:
            break
    return ascii_slug("".join(x[:1].upper() + x[1:] for x in ap))


def nuevo_nombre(d: dict) -> str:
    autores = [a.strip() for a in d["autores"].split(";") if a.strip()]
    firma = "-".join(apellido(a) for a in autores[:2]) + ("-etal" if len(autores) > 2 else "")
    num = f"PdR{int(d['numero']):03d}" if d["numero"].isdigit() else "PdR"
    partes = [d["anio"] or "s-f", num, firma or "CEP"]
    return "_".join(p for p in partes if p) + ".pdf"


def extract(args) -> None:
    data = Path(args.data)
    raw = data / "raw"
    filas = []
    for ficha in leer_csv(data / "catalogo.csv"):
        pdf = raw / f"{ficha['id']}.pdf"
        base = {**ficha, "archivo": pdf.name}
        if not pdf.exists():
            filas.append({**base, "estado": "sin_pdf"})
            continue
        try:
            texto = texto_portada(PdfReader(pdf))
        except Exception as e:
            texto = ""
            print(f"  {pdf.name}: ilegible ({e.__class__.__name__})", file=sys.stderr)
        p = extraer(texto)
        d = {k: ficha.get(k) or p.get(k, "") for k in ("numero", "anio", "mes", "titulo", "autores")}
        d["area"], d["palabras_clave"] = p["area"], p["palabras_clave"]
        faltan = [c for c in ("numero", "anio", "titulo", "autores") if not d[c]]
        avisos = ["falta " + ", ".join(faltan)] if faltan else []
        if p["numero"] and ficha["numero"] and p["numero"] != ficha["numero"]:
            avisos.append(f"el PDF dice N° {p['numero']}")
        estado = "revisar: " + "; ".join(avisos) if avisos else "ok"
        filas.append({**base, **d, "estado": estado, "_num_pdf": p["numero"]})

    # Correcciones revisadas a mano (id, campo, valor, fuente). Corregir una fila la da por revisada.
    ruta = Path(args.correcciones)
    if ruta.exists():
        por_id = {f["id"]: f for f in filas}
        for c in leer_csv(ruta):
            f = por_id.get(c["id"])
            if f is None or f["estado"] == "sin_pdf":
                continue
            f[c["campo"]] = c["valor"]
            if c["campo"] != "estado":
                f["estado"] = "ok"

    # Hay fichas que enlazan el mismo archivo: reediciones de la misma ficha, o una
    # ficha que apunta al PDF de otro número (la del N° 105 enlaza el del N° 106).
    # Se queda la ficha cuyo número coincide con el impreso en el PDF.
    grupos: dict[str, list[dict]] = {}
    for f in filas:
        if f["estado"] == "ok":
            h = hashlib.sha1((raw / f["archivo"]).read_bytes()).hexdigest()
            grupos.setdefault(h, []).append(f)
    for grupo in grupos.values():
        grupo.sort(key=lambda f: f["_num_pdf"] != f["numero"])
        for f in grupo[1:]:
            f["estado"] = f"duplicado: mismo PDF que la ficha {grupo[0]['id']} (N° {grupo[0]['numero']})"

    # Nombres repetidos (reediciones, números duplicados): se desambiguan con sufijo.
    usados: dict[str, int] = {}
    for f in filas:
        if f["estado"] == "sin_pdf" or f["estado"].startswith(("duplicado", "excluido")):
            continue
        n = nuevo_nombre(f)
        usados[n] = usados.get(n, 0) + 1
        f["nuevo_nombre"] = n if usados[n] == 1 else n[:-4] + f"_{usados[n]}.pdf"

    destino = data / "manifest.csv"
    escribir_csv(destino, MANIFEST_FIELDS, filas)
    resumen: dict[str, int] = {}
    for f in filas:
        clave = f["estado"].split(":")[0]
        resumen[clave] = resumen.get(clave, 0) + 1
    print(f"{len(filas)} fichas -> {destino}  {resumen}")


# ------------------------------------------------------------------ escritura

def asunto(d: dict) -> str:
    fecha = ""
    if d["mes"].isdigit() and d["anio"]:
        nombre_mes = next(k for k, v in MESES.items() if v == int(d["mes"]))
        fecha = f", {nombre_mes} {d['anio']}"
    elif d["anio"]:
        fecha = f", {d['anio']}"
    area = f". {d['area']}" if d.get("area") else ""
    return f"Puntos de Referencia N° {d['numero']}{fecha}{area}. Centro de Estudios Públicos."


def xmp(d: dict, fecha: str) -> bytes:
    autores = [a.strip() for a in d["autores"].split(";") if a.strip()]
    claves = [c.strip() for c in d["palabras_clave"].split(",") if c.strip()]
    li = lambda xs: "".join(f"<rdf:li>{escape(x)}</rdf:li>" for x in xs)
    fecha_xml = f"<dc:date><rdf:Seq><rdf:li>{fecha}</rdf:li></rdf:Seq></dc:date>" if fecha else ""
    return f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:pdf="http://ns.adobe.com/pdf/1.3/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
   <dc:format>application/pdf</dc:format>
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{escape(d['titulo'])}</rdf:li></rdf:Alt></dc:title>
   <dc:creator><rdf:Seq>{li(autores)}</rdf:Seq></dc:creator>
   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{escape(asunto(d))}</rdf:li></rdf:Alt></dc:description>
   <dc:subject><rdf:Bag>{li(claves)}</rdf:Bag></dc:subject>
   <dc:publisher><rdf:Bag><rdf:li>Centro de Estudios Públicos</rdf:li></rdf:Bag></dc:publisher>
   <dc:language><rdf:Bag><rdf:li>es</rdf:li></rdf:Bag></dc:language>
   {fecha_xml}
   <pdf:Keywords>{escape(d['palabras_clave'])}</pdf:Keywords>
   <prism:publicationName>Puntos de Referencia</prism:publicationName>
   <prism:number>{escape(d['numero'])}</prism:number>
   <prism:url>{escape(d.get('page_url', ''))}</prism:url>
   <xmp:MetadataDate>{datetime.now().astimezone().isoformat(timespec='seconds')}</xmp:MetadataDate>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>""".encode("utf-8")


def apply(args) -> None:
    data = Path(args.data)
    raw, final = data / "raw", data / "final"
    final.mkdir(parents=True, exist_ok=True)
    hechos = omitidos = fallos = 0
    for f in leer_csv(data / "manifest.csv"):
        if f["estado"] != "ok" and not (args.include_review and f["estado"].startswith("revisar")):
            omitidos += 1
            continue
        anio = f["anio"] if f["anio"].isdigit() else ""
        mes = int(f["mes"]) if f["mes"].isdigit() else 1
        info = {
            "/Title": f["titulo"],
            "/Author": f["autores"],
            "/Subject": asunto(f),
            "/Keywords": f["palabras_clave"],
            "/Creator": "Centro de Estudios Públicos",
        }
        if anio:
            info["/CreationDate"] = f"D:{anio}{mes:02d}01000000"
        try:
            writer = PdfWriter(clone_from=str(raw / f["archivo"]))
            writer.add_metadata(info)
            # El XMP manda en Acrobat, Zotero y la mayoría de los lectores:
            # si queda el de InDesign, el título viejo reaparece.
            stream = DecodedStreamObject()
            stream.set_data(xmp(f, f"{anio}-{mes:02d}" if anio else ""))
            stream.update({NameObject("/Type"): NameObject("/Metadata"),
                           NameObject("/Subtype"): NameObject("/XML")})
            writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
            with (final / f["nuevo_nombre"]).open("wb") as out:
                writer.write(out)
            hechos += 1
        except Exception as e:
            fallos += 1
            print(f"  {f['archivo']}: no se pudo escribir ({e.__class__.__name__}: {e})", file=sys.stderr)
    print(f"{hechos} PDF escritos en {final}; {omitidos} omitidos; {fallos} fallos")


# ----------------------------------------------------------------------- CLI

def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data", help="carpeta de trabajo (por defecto: data)")
    p.add_argument("--delay", type=float, default=0.5, help="segundos entre solicitudes")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog", help="lee las fichas de la serie desde la API")
    sub.add_parser("download", help="descarga los PDF del catálogo")
    ex = sub.add_parser("extract", help="cruza fichas y portadas en manifest.csv")
    ap = sub.add_parser("apply", help="escribe metadatos y renombra en data/final")
    ap.add_argument("--include-review", action="store_true",
                    help="aplicar también las filas marcadas «revisar»")
    al = sub.add_parser("all", help="catalog + download + extract")
    for sp in (ex, al):
        sp.add_argument("--correcciones", default=str(Path(__file__).with_name("correcciones.csv")),
                        help="CSV de correcciones manuales (id, campo, valor, fuente)")

    args = p.parse_args(argv)
    if args.cmd == "all":
        catalog(args)
        download(args)
        extract(args)
        print("Revise data/manifest.csv y luego corra: python cep_pdr.py apply")
    else:
        {"catalog": catalog, "download": download, "extract": extract, "apply": apply}[args.cmd](args)


if __name__ == "__main__":
    main()
