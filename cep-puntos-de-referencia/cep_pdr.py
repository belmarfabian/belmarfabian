#!/usr/bin/env python3
"""Descarga la serie Puntos de Referencia del CEP y corrige los metadatos de cada PDF.

Flujo en cuatro pasos, cada uno retomable:

  crawl     recorre cepchile.cl y lista las URL de PDF candidatas   -> data/urls.csv
  download  descarga las candidatas                                 -> data/raw/
  extract   lee la portada de cada PDF y propone metadatos          -> data/manifest.csv
  apply     escribe metadatos (Info + XMP) y copia renombrada       -> data/final/

El manifiesto es editable: si la extracción se equivoca en un título o un autor,
se corrige la fila y se vuelve a correr `apply`. Los originales no se tocan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import time
import unicodedata
import urllib.robotparser
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

BASE = "https://www.cepchile.cl/"
DOMAINS = ("cepchile.cl",)
UA = "Mozilla/5.0 (compatible; cep-pdr-archiver/1.0; investigacion academica)"
PDR_HINT = re.compile(r"puntos?[\s_-]*de[\s_-]*referencia|\bpd[e]?r[\s_-]?\d{2,4}", re.I)

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
PARTICULAS = {"de", "del", "la", "las", "los", "y", "e", "van", "von", "da", "di"}
MAYUS = "A-ZÁÉÍÓÚÑÜ"

URL_FIELDS = ["pdf_url", "page_url", "page_title", "hint"]
MANIFEST_FIELDS = [
    "archivo", "numero", "anio", "mes", "area", "titulo", "autores",
    "palabras_clave", "nuevo_nombre", "estado", "pdf_url",
]


# --------------------------------------------------------------------------- red

def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def get(s: requests.Session, url: str, delay: float, **kw) -> requests.Response | None:
    for intento in range(4):
        try:
            r = s.get(url, timeout=60, **kw)
            time.sleep(delay)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 410):
                return None
        except requests.RequestException as e:
            print(f"  error de red ({e.__class__.__name__}) en {url}", file=sys.stderr)
        time.sleep(2 ** (intento + 1))
    return None


def en_dominio(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in DOMAINS)


def sitemaps(s: requests.Session, delay: float) -> list[str]:
    """URL de páginas declaradas en robots.txt y en los sitemaps habituales."""
    candidatos = [urljoin(BASE, "sitemap.xml"), urljoin(BASE, "sitemap_index.xml")]
    r = get(s, urljoin(BASE, "robots.txt"), delay)
    if r:
        candidatos += re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)
    vistos, paginas, cola = set(), [], deque(dict.fromkeys(candidatos))
    while cola:
        sm = cola.popleft()
        if sm in vistos:
            continue
        vistos.add(sm)
        r = get(s, sm, delay)
        if not r:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
        if "<sitemapindex" in r.text:
            cola.extend(locs)
        else:
            paginas.extend(locs)
    return paginas


def crawl(args) -> None:
    s = session()
    robots = urllib.robotparser.RobotFileParser(urljoin(BASE, "robots.txt"))
    try:
        robots.read()
    except Exception:
        robots = None

    semillas = list(args.seed or [])
    if not args.no_sitemap:
        desde_sitemap = sitemaps(s, args.delay)
        print(f"sitemaps: {len(desde_sitemap)} páginas")
        semillas += desde_sitemap
    if not semillas:
        semillas = [BASE]

    follow = re.compile(args.follow, re.I) if args.follow else None
    cola, vistas = deque(dict.fromkeys(semillas)), set()
    pdfs: dict[str, dict] = {}
    while cola and len(vistas) < args.max_pages:
        url = urldefrag(cola.popleft())[0]
        if url in vistas or not en_dominio(url):
            continue
        if robots and not robots.can_fetch(UA, url):
            continue
        vistas.add(url)
        r = get(s, url, args.delay)
        if not r or "html" not in r.headers.get("content-type", ""):
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        cuerpo = soup.find("main") or soup.find("article") or soup.body or soup
        texto_pagina = cuerpo.get_text(" ", strip=True)
        titulo_pagina = meta(soup, "citation_title", "og:title") or (soup.title.string if soup.title else "")
        for a in soup.find_all("a", href=True):
            link = urldefrag(urljoin(url, a["href"]))[0]
            if not en_dominio(link):
                continue
            if urlparse(link).path.lower().endswith(".pdf"):
                pista = bool(PDR_HINT.search(link) or PDR_HINT.search(a.get_text(" "))
                             or PDR_HINT.search(texto_pagina))
                previo = pdfs.get(link)
                if previo is None or (pista and not previo["hint"]):
                    pdfs[link] = {"pdf_url": link, "page_url": url,
                                  "page_title": (titulo_pagina or "").strip(), "hint": int(pista)}
            elif link not in vistas and (follow is None or follow.search(link)):
                cola.append(link)
        if len(vistas) % 100 == 0:
            print(f"  {len(vistas)} páginas, {len(pdfs)} PDF, {len(cola)} en cola")

    salida = Path(args.data) / "urls.csv"
    salida.parent.mkdir(parents=True, exist_ok=True)
    with salida.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=URL_FIELDS)
        w.writeheader()
        w.writerows(sorted(pdfs.values(), key=lambda d: d["pdf_url"]))
    n_pista = sum(d["hint"] for d in pdfs.values())
    print(f"{len(vistas)} páginas recorridas; {len(pdfs)} PDF, {n_pista} con pista de PdR -> {salida}")


def meta(soup: BeautifulSoup, *nombres: str) -> str:
    for n in nombres:
        tag = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def nombre_local(url: str) -> str:
    base = Path(urlparse(url).path).name or "archivo.pdf"
    h = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{Path(base).stem[:80]}_{h}.pdf"


def download(args) -> None:
    urls = Path(args.data) / "urls.csv"
    raw = Path(args.data) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    filas = list(csv.DictReader(urls.open(encoding="utf-8")))
    if not args.all_pdfs:
        filas = [f for f in filas if f["hint"] == "1"]
    s = session()
    for i, fila in enumerate(filas, 1):
        destino = raw / nombre_local(fila["pdf_url"])
        if destino.exists() and destino.stat().st_size > 0:
            continue
        r = get(s, fila["pdf_url"], args.delay)
        if r is None or not r.content.startswith(b"%PDF"):
            print(f"  [{i}/{len(filas)}] falló {fila['pdf_url']}", file=sys.stderr)
            continue
        destino.write_bytes(r.content)
        print(f"  [{i}/{len(filas)}] {destino.name}")


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


def extraer(texto: str, titulo_html: str = "", titulo_info: str = "") -> dict:
    t = normalizar(texto)
    d = {"numero": "", "anio": "", "mes": "", "area": "", "titulo": "", "autores": "", "palabras_clave": ""}

    # El encabezado se repite en cada página («N° 766, ABRIL 2026 ÁREA PUNTOS DE REFERENCIA»);
    # en la portada suele venir entremezclado con otras columnas, así que se revisan todos.
    for m in re.finditer(rf"N[°º o.]*\s*(\d{{1,4}}),?\s+([{MAYUS}a-záéíóúñ]+)\s+(\d{{4}})", t):
        if not d["numero"]:
            d["numero"], d["anio"] = m.group(1), m.group(3)
            d["mes"] = str(MESES.get(m.group(2).lower(), ""))
        a = re.match(rf"\s*([{MAYUS} ,]+?)\s+PUNTOS DE REFERENCIA", t[m.end():m.end() + 120])
        if a and a.group(1).strip():
            d["area"] = capitalizar(a.group(1))
            break

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

    titulo = titulo_html.strip()
    if not titulo and pos_autores > 0:
        previas = [l.strip() for l in t[:pos_autores].splitlines() if l.strip()]
        bloque = []
        for l in reversed(previas):
            if re.search(r"PUNTOS DE REFERENCIA|puntos de referencia|N°\s*\d|^[{0}\s,]+$".format(MAYUS), l):
                break
            bloque.insert(0, l)
            if len(bloque) >= 4:
                break
        titulo = " ".join(bloque)
    if not titulo and titulo_info and not re.search(r"\.indd|untitled|sin t[ií]tulo|^pdr", titulo_info, re.I):
        titulo = titulo_info.strip()
    d["titulo"] = re.sub(r"\s+", " ", titulo).strip(" .")

    k = re.search(r"Palabras\s+clave\s*:\s*(.+?)(?:\n\s*\n|\n[{0}]{{3,}}|$)".format(MAYUS), t, re.S)
    if k:
        claves = [c.strip(" .\n") for c in re.split(r"[,;]", k.group(1).replace("\n", " "))]
        d["palabras_clave"] = ", ".join(c for c in claves if c)
    return d


def ascii_slug(t: str) -> str:
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-")


def apellido(nombre: str) -> str:
    partes = nombre.split()
    if not partes:
        return ""
    ap = [partes[-1]]
    for p in reversed(partes[:-1]):  # «de la Fuente», «van der Berg»
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
    urls = {}
    if (data / "urls.csv").exists():
        for f in csv.DictReader((data / "urls.csv").open(encoding="utf-8")):
            urls[nombre_local(f["pdf_url"])] = f
    raw = Path(args.raw) if args.raw else data / "raw"
    filas = []
    for pdf in sorted(raw.glob("*.pdf")):
        origen = urls.get(pdf.name, {})
        try:
            reader = PdfReader(pdf)
            texto = texto_portada(reader)
            info_titulo = (reader.metadata or {}).get("/Title", "") or ""
        except Exception as e:
            filas.append({"archivo": pdf.name, "estado": f"ilegible: {e.__class__.__name__}"})
            continue
        es_pdr = bool(re.search(r"puntos\s+de\s+referencia", texto, re.I))
        titulo_html = origen.get("page_title", "") if args.use_html_title else ""
        d = extraer(texto, titulo_html=titulo_html, titulo_info=str(info_titulo))
        faltan = [c for c in ("numero", "anio", "titulo", "autores") if not d[c]]
        estado = "no_pdr" if not es_pdr else ("revisar: falta " + ", ".join(faltan) if faltan else "ok")
        filas.append({"archivo": pdf.name, **d, "nuevo_nombre": nuevo_nombre(d),
                      "estado": estado, "pdf_url": origen.get("pdf_url", "")})

    # Nombres repetidos (p. ej., dos PdR sin número): se desambiguan con sufijo.
    usados: dict[str, int] = {}
    for f in filas:
        n = f.get("nuevo_nombre")
        if not n:
            continue
        usados[n] = usados.get(n, 0) + 1
        if usados[n] > 1:
            f["nuevo_nombre"] = n[:-4] + f"_{usados[n]}.pdf"

    salida = data / "manifest.csv"
    salida.parent.mkdir(parents=True, exist_ok=True)
    with salida.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerows(filas)
    resumen = {}
    for f in filas:
        clave = f["estado"].split(":")[0]
        resumen[clave] = resumen.get(clave, 0) + 1
    print(f"{len(filas)} PDF -> {salida}  {resumen}")


# ------------------------------------------------------------------ escritura

def xmp(d: dict, fecha: str) -> bytes:
    autores = [a.strip() for a in d["autores"].split(";") if a.strip()]
    claves = [c.strip() for c in d["palabras_clave"].split(",") if c.strip()]
    li = lambda xs: "".join(f"<rdf:li>{escape(x)}</rdf:li>" for x in xs)
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
   <dc:date><rdf:Seq><rdf:li>{fecha}</rdf:li></rdf:Seq></dc:date>
   <pdf:Keywords>{escape(d['palabras_clave'])}</pdf:Keywords>
   <prism:publicationName>Puntos de Referencia</prism:publicationName>
   <prism:number>{escape(d['numero'])}</prism:number>
   <prism:url>{escape(d.get('pdf_url', ''))}</prism:url>
   <xmp:MetadataDate>{datetime.now().astimezone().isoformat(timespec='seconds')}</xmp:MetadataDate>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>""".encode("utf-8")


def asunto(d: dict) -> str:
    fecha = ""
    if d["mes"].isdigit() and d["anio"]:
        nombre_mes = next(k for k, v in MESES.items() if v == int(d["mes"]))
        fecha = f", {nombre_mes} {d['anio']}"
    elif d["anio"]:
        fecha = f", {d['anio']}"
    area = f". {d['area']}" if d["area"] else ""
    return f"Puntos de Referencia N° {d['numero']}{fecha}{area}. Centro de Estudios Públicos."


def apply(args) -> None:
    data = Path(args.data)
    raw = Path(args.raw) if args.raw else data / "raw"
    final = data / "final"
    final.mkdir(parents=True, exist_ok=True)
    filas = list(csv.DictReader((data / "manifest.csv").open(encoding="utf-8")))
    hechos = omitidos = 0
    for f in filas:
        f = {k: (v or "") for k, v in f.items()}
        if f["estado"] == "no_pdr" or f["estado"].startswith("ilegible") or \
                (f["estado"].startswith("revisar") and not args.include_review):
            omitidos += 1
            continue
        anio = f["anio"] if f["anio"].isdigit() else ""
        mes = int(f["mes"]) if f["mes"].isdigit() else 1
        fecha_iso = f"{anio}-{mes:02d}" if anio else ""
        writer = PdfWriter(clone_from=str(raw / f["archivo"]))
        info = {
            "/Title": f["titulo"],
            "/Author": f["autores"],
            "/Subject": asunto(f),
            "/Keywords": f["palabras_clave"],
            "/Creator": "Centro de Estudios Públicos",
        }
        if anio:
            info["/CreationDate"] = f"D:{anio}{mes:02d}01000000"
        writer.add_metadata(info)
        # El XMP manda en Acrobat, Zotero y la mayoría de los lectores:
        # si queda el de InDesign, el título viejo reaparece.
        stream = DecodedStreamObject()
        stream.set_data(xmp(f, fecha_iso))
        stream.update({NameObject("/Type"): NameObject("/Metadata"),
                       NameObject("/Subtype"): NameObject("/XML")})
        writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
        with (final / f["nuevo_nombre"]).open("wb") as out:
            writer.write(out)
        hechos += 1
    print(f"{hechos} PDF escritos en {final}; {omitidos} omitidos (no PdR o por revisar)")


# ----------------------------------------------------------------------- CLI

def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data", help="carpeta de trabajo (por defecto: data)")
    p.add_argument("--delay", type=float, default=1.0, help="segundos entre solicitudes")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="lista los PDF candidatos")
    c.add_argument("--seed", action="append", help="URL inicial (repetible)")
    c.add_argument("--follow", default="", help="regex: solo seguir páginas que coincidan")
    c.add_argument("--max-pages", type=int, default=20000)
    c.add_argument("--no-sitemap", action="store_true")

    dl = sub.add_parser("download", help="descarga los PDF con pista de PdR")
    dl.add_argument("--all-pdfs", action="store_true", help="descargar también los sin pista")

    for nombre, ayuda in (("extract", "propone metadatos en manifest.csv"),
                          ("apply", "escribe metadatos y renombra en data/final")):
        sp = sub.add_parser(nombre, help=ayuda)
        sp.add_argument("--raw", help="carpeta de PDF de entrada (por defecto: data/raw)")
        if nombre == "extract":
            sp.add_argument("--use-html-title", action="store_true",
                            help="preferir el título de la página web al de la portada")
        else:
            sp.add_argument("--include-review", action="store_true",
                            help="aplicar también las filas marcadas «revisar»")

    a = sub.add_parser("all", help="crawl + download + extract")
    a.add_argument("--seed", action="append")
    a.add_argument("--follow", default="")
    a.add_argument("--max-pages", type=int, default=20000)
    a.add_argument("--no-sitemap", action="store_true")
    a.add_argument("--all-pdfs", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "all":
        crawl(args)
        download(args)
        args.raw, args.use_html_title = None, False
        extract(args)
        print("Revise data/manifest.csv y luego corra: python cep_pdr.py apply")
    else:
        {"crawl": crawl, "download": download, "extract": extract, "apply": apply}[args.cmd](args)


if __name__ == "__main__":
    main()
