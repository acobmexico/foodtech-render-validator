import ipaddress
import os
import re
import socket
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from pydantic import BaseModel, Field


MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
REQUEST_TIMEOUT = int(os.getenv("WEB_REQUEST_TIMEOUT", "20"))
MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 5
MAX_CHARS_MAIN = 18_000
MAX_CHARS_CORPORATE = 12_000
USER_AGENT = "Mozilla/5.0 (compatible; FoodtechQualification/3.0; +Render)"

SOCIAL_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "tiktok.com",
    "x.com", "twitter.com", "youtube.com",
}

SEARCH_ENGINE_HOSTS = {
    "google.com", "google.com.mx", "googleusercontent.com",
    "bing.com", "search.yahoo.com",
}

GIROS_REFERENCIA = """
Agricultura, apicultura, avicultura, ganadería, pesca, aceites comestibles,
aderezos, alimentos artesanales, congelados, deshidratados, enlatados,
alimentos para animales o bebés, alimentos procesados, agua, bebidas,
botanas, embutidos, carnes, cereales, chocolates, confitería, envases,
empaques, esencias, especias, condimentos, etiquetas, gases y petroquímica
para alimentos, harinas, granos, ingenios azucareros, ingredientes, aditivos,
laboratorios para alimentos, lácteos, materias primas, maquinaria y tecnología
para procesamiento, mermeladas, panificados, pastas, químicos alimentarios,
sabores, fragancias, salsas y suplementos alimenticios.
""".strip()

SYSTEM_PROMPT = f"""
Eres un comité de admisión B2B extremadamente estricto para una exposición
especializada en ingredientes, aditivos, soluciones tecnológicas, procesamiento
y empaque para la industria de alimentos y bebidas.

Evalúa exclusivamente el GIRO Y LA ACTIVIDAD ACTUAL DE LA EMPRESA. Ignora por
completo puestos, cargos, áreas, autoridad individual o departamentos.

Determina si la empresa participa profesionalmente en el ecosistema FOODTECH. La
vía principal es comprar, fabricar, transformar, integrar, representar o
distribuir ingredientes, insumos, maquinaria, tecnología, envases o servicios
relacionados. Existe además una vía válida e independiente: ser un MEDIO
ESPECIALIZADO cuya actividad editorial demostrada se concentre en la industria
de alimentos y bebidas o en su cadena de ingredientes, procesamiento, empaque,
calidad y tecnología.

GIROS ORIENTATIVOS NO LIMITATIVOS:
{GIROS_REFERENCIA}

REGLAS OBLIGATORIAS:
1. Usa únicamente el contenido de la página proporcionada y, si existe, una sola
   página corporativa enlazada desde ella.
2. No uses conocimiento externo, memoria, directorios ni información no incluida.
3. No supongas actividades que las páginas no demuestren.
4. Clasifica como COMPRADOR, DISTRIBUIDOR, AMBOS, MEDIO_ESPECIALIZADO o
   NO_CALIFICA.
5. COMPRADOR necesita adquirir naturalmente soluciones objetivo por su operación.
6. DISTRIBUIDOR comercializa, importa, representa, distribuye o integra soluciones
   relevantes para alimentos y bebidas.
7. AMBOS consume o transforma y además distribuye a otras organizaciones.
8. Palabras genéricas como alimentos, tecnología o calidad no bastan; debe existir
   una relación comercial demostrada.
9. Fabricantes, procesadores, empacadores, cadenas de restaurantes, cadenas
   hoteleras con operación alimentaria, comedores industriales, importadores,
   mayoristas y distribuidores especializados pueden obtener calificación alta.
10. MEDIO ESPECIALIZADO: aprueba un medio editorial, revista, portal de noticias,
    publicación, directorio sectorial o plataforma de contenido cuando las páginas
    demuestren que su enfoque editorial y su audiencia profesional pertenecen
    específicamente a alimentos y bebidas, ingredientes, procesamiento, empaque,
    inocuidad, calidad o tecnología alimentaria. No es necesario que compre,
    fabrique o distribuya productos. Asígnale al menos 65 puntos cuando la
    especialización FOODTECH esté demostrada y clasifícalo MEDIO_ESPECIALIZADO.
11. No apruebes un medio solamente por publicar una nota aislada de alimentos.
    Debe existir evidencia de especialización editorial recurrente o de que su
    directorio, noticias, capacitación o contenido están dirigidos al sector.
12. Rechaza medios generalistas y medios especializados en industrias ajenas,
    por ejemplo automotriz, construcción, moda o entretenimiento, aunque sean
    medios editoriales. Clasifícalos NO_CALIFICA salvo que exista en las páginas
    una división FOODTECH clara, actual y sustancial.
13. Universidades, asociaciones, consultores, gobierno, despachos, escuelas y
    servicios generales deben recibir calificación baja salvo evidencia directa
    de compra, distribución o una actividad sectorial expresamente admitida.
14. Si no existe evidencia suficiente del giro, asigna una calificación baja o 0.
15. Escala estricta: 100 inequívoco; 80-99 relación sólida; 60-79 relevante pero
    parcial; 30-59 secundaria; 1-29 débil; 0 sin evidencia suficiente.
16. Para un medio FOODTECH inequívoco usa normalmente 70-90 puntos. Reserva
    65-69 para especialización válida pero con evidencia limitada.
17. La razón debe estar en español y contener como máximo 50 palabras.
18. No menciones puestos ni sugieras que faltó conocerlos.

EJEMPLOS DE DECISIÓN:
- Revista o portal con directorio, noticias y capacitación para profesionales de
  alimentos, ingredientes o procesamiento: MEDIO_ESPECIALIZADO y aprobado.
- Revista automotriz sin una división alimentaria demostrada: NO_CALIFICA.
- Portal general de noticias que ocasionalmente publica sobre comida: NO_CALIFICA.
""".strip()


class CompanyEvaluation(BaseModel):
    score: int = Field(ge=0, le=100)
    type: Literal[
        "COMPRADOR", "DISTRIBUIDOR", "AMBOS", "MEDIO_ESPECIALIZADO",
        "NO_CALIFICA",
    ]
    detected_business: str
    evidence: str
    reason: str


_thread_local = threading.local()


def get_openai_client() -> OpenAI:
    if not hasattr(_thread_local, "openai_client"):
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY no está configurada.")
        _thread_local.openai_client = OpenAI(
            api_key=api_key, timeout=120.0, max_retries=2
        )
    return _thread_local.openai_client


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def limit_words(text: str, maximum: int = 50) -> str:
    return " ".join(re.findall(r"\S+", (text or "").strip())[:maximum])


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("La empresa no proporcionó página web.")
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url
    url = unwrap_search_redirect(url)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("La URL no es válida.")
    if parsed.username or parsed.password:
        raise ValueError("La URL no puede incluir credenciales.")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("El puerto de la URL no está permitido.")
    return url


def is_search_engine_host(host: str) -> bool:
    normalized = (host or "").casefold().rstrip(".")
    normalized = normalized[4:] if normalized.startswith("www.") else normalized
    return any(
        normalized == item or normalized.endswith("." + item)
        for item in SEARCH_ENGINE_HOSTS
    )


def unwrap_search_redirect(url: str) -> str:
    """Extrae el destino de enlaces de salida sin consultar al buscador."""
    parsed = urlparse(url)
    if not is_search_engine_host(parsed.hostname or ""):
        return url
    query = parse_qs(parsed.query)
    candidates = query.get("q", []) + query.get("url", []) + query.get("u", [])
    for candidate in candidates:
        destination = unquote(candidate).strip()
        if re.match(r"^https?://", destination, flags=re.I):
            target = urlparse(destination)
            if target.hostname and not is_search_engine_host(target.hostname):
                return destination
    raise ValueError(
        "Se recibió una página de búsqueda de Google/Bing en lugar del sitio "
        "directo de la empresa. No se consulta al buscador para evitar CAPTCHA."
    )


def normalized_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_domain(left: str, right: str) -> bool:
    return normalized_domain(left) == normalized_domain(right)


def reject_private_destination(url: str) -> None:
    host = urlparse(url).hostname
    if not host or host.casefold() in {"localhost", "localhost.localdomain"}:
        raise ValueError("No se permiten direcciones locales.")
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("No fue posible localizar el dominio.") from exc
    for result in results:
        ip = ipaddress.ip_address(result[4][0])
        if any((ip.is_private, ip.is_loopback, ip.is_link_local,
                ip.is_reserved, ip.is_multicast, ip.is_unspecified)):
            raise ValueError("La URL apunta a una red privada o reservada.")


def is_social_url(url: str) -> bool:
    host = normalized_domain(url)
    return any(host == item or host.endswith("." + item) for item in SOCIAL_HOSTS)


def download_html(url: str) -> tuple[str, str]:
    current = normalize_url(url)
    if is_social_url(current):
        raise ValueError("La URL corresponde a una red social no procesable.")
    session = requests.Session()
    for _ in range(MAX_REDIRECTS + 1):
        if is_search_engine_host(urlparse(current).hostname or ""):
            raise ValueError(
                "El sitio redirigió a un buscador; se bloqueó la solicitud para "
                "evitar rate-limiting y CAPTCHA."
            )
        if is_social_url(current):
            raise ValueError("La URL redirigió a una red social no procesable.")
        reject_private_destination(current)
        response = session.get(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "es-MX,es;q=0.9,en;q=0.7",
                "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
            },
            timeout=(7, REQUEST_TIMEOUT),
            allow_redirects=False,
            stream=True,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            response.close()
            if not location:
                raise ValueError("La redirección no incluye destino.")
            current = normalize_url(urljoin(current, location))
            continue
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").casefold()
        if "html" not in content_type and "text" not in content_type:
            response.close()
            raise ValueError("La URL no devolvió una página de texto.")
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=65_536, decode_unicode=False):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                response.close()
                raise ValueError("La página supera el tamaño máximo permitido.")
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        final_url = response.url
        response.close()
        return final_url, b"".join(chunks).decode(encoding, errors="replace")
    raise ValueError("La página excedió el máximo de redirecciones.")


def extract_visible_text(html: str, limit: int) -> tuple[str, int]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = meta.get("content", "") if meta else ""
    for tag in soup(["script", "style", "noscript", "svg", "template", "form"]):
        tag.decompose()
    content = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    result = f"TÍTULO: {title}\nDESCRIPCIÓN: {description}\nCONTENIDO: {content}"
    return result[:limit], len(content)


CORPORATE_EXPRESSIONS = [
    "nosotros", "acerca de nosotros", "sobre nosotros", "quienes somos",
    "conocenos", "nuestra empresa", "about us", "who we are",
    "our company", "company profile", "about the company",
]


def corporate_link_score(anchor_text: str, href: str) -> int:
    text = normalize_text(anchor_text)
    path = normalize_text(urlparse(href).path.replace("-", " ").replace("_", " "))
    excluded = {
        "facebook", "instagram", "linkedin", "youtube", "contacto", "contact",
        "blog", "noticias", "news", "empleo", "careers", "privacy", "privacidad",
    }
    if any(word in f"{text} {path}" for word in excluded):
        return 0
    score = 0
    exact = {
        "nosotros": 100, "acerca de nosotros": 100, "sobre nosotros": 100,
        "quienes somos": 100, "conocenos": 95, "about us": 100,
        "who we are": 100, "our company": 95, "nuestra empresa": 95,
        "company profile": 90,
    }
    score = max(score, exact.get(text, 0))
    for expression in CORPORATE_EXPRESSIONS:
        normalized = normalize_text(expression)
        if normalized in text:
            score = max(score, 85)
        if normalized in path:
            score = max(score, 70)
    if urlparse(href).path.casefold().rstrip("/") in {
        "/about", "/about-us", "/nosotros", "/quienes-somos", "/acerca-de",
        "/conocenos", "/empresa", "/company",
    }:
        score = max(score, 80)
    return score


def find_corporate_page(html: str, main_url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        raw = anchor.get("href", "").strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urldefrag(urljoin(main_url, raw)).url
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not same_domain(main_url, absolute):
            continue
        if absolute.rstrip("/") == main_url.rstrip("/") or absolute in seen:
            continue
        seen.add(absolute)
        score = corporate_link_score(anchor.get_text(" ", strip=True), absolute)
        if score:
            candidates.append({"score": score, "url": absolute})
    return max(candidates, key=lambda item: item["score"]) if candidates else None


def collect_company_information(url: str) -> dict:
    final_url, main_html = download_html(url)
    main_text, main_chars = extract_visible_text(main_html, MAX_CHARS_MAIN)
    corporate_candidate = find_corporate_page(main_html, final_url)
    corporate_url, corporate_text, corporate_chars, corporate_error = "", "", 0, ""
    if corporate_candidate:
        try:
            found_url, found_html = download_html(corporate_candidate["url"])
            if not same_domain(final_url, found_url):
                raise ValueError("La página corporativa redirigió a otro dominio.")
            corporate_text, corporate_chars = extract_visible_text(
                found_html, MAX_CHARS_CORPORATE
            )
            corporate_url = found_url
        except Exception as exc:
            corporate_error = f"{type(exc).__name__}: {exc}"
    return {
        "main_url": final_url,
        "main_text": main_text,
        "main_chars": main_chars,
        "corporate_url": corporate_url,
        "corporate_text": corporate_text,
        "corporate_chars": corporate_chars,
        "corporate_error": corporate_error,
    }


def no_evidence_evaluation(reason: str) -> dict:
    concise = limit_words(
        "No fue posible obtener evidencia verificable del giro de la empresa en "
        f"la página proporcionada. {reason}", 50
    )
    return {
        "score": 0,
        "type": "NO_CALIFICA",
        "detected_business": "No identificable con evidencia web disponible",
        "evidence": "No se obtuvo contenido web verificable.",
        "reason": concise,
        "main_url": "",
        "corporate_url": "",
        "web_error": reason,
    }


def evaluate_record(record: dict, threshold: int) -> dict:
    page = str(record.get("PaginaWeb") or "").strip()
    try:
        information = collect_company_information(page)
    except Exception as exc:
        evaluation = no_evidence_evaluation(f"{type(exc).__name__}: {exc}")
    else:
        corporate_block = (
            f"PÁGINA CORPORATIVA ADICIONAL:\nURL: {information['corporate_url']}\n"
            f"{information['corporate_text']}"
            if information["corporate_url"]
            else "No se obtuvo una página corporativa adicional."
        )
        company_name = str(record.get("RazonSocial") or "").strip()
        user_input = f"""
RAZÓN SOCIAL DECLARADA: {company_name}
URL PROPORCIONADA: {information['main_url']}

CONTENIDO DE LA PÁGINA PROPORCIONADA:
---
{information['main_text']}
---

{corporate_block}

Evalúa exclusivamente el giro y la actividad actual demostrados por estas páginas.
No consideres Cargo ni Area aunque existan en el registro.
""".strip()
        response = get_openai_client().responses.parse(
            model=MODEL,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_input},
            ],
            text_format=CompanyEvaluation,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI no devolvió una evaluación estructurada.")
        evaluation = {
            "score": int(parsed.score),
            "type": parsed.type,
            "detected_business": parsed.detected_business,
            "evidence": parsed.evidence,
            "reason": limit_words(parsed.reason, 50),
            "main_url": information["main_url"],
            "corporate_url": information["corporate_url"],
            "web_error": information["corporate_error"],
        }

    allowed = evaluation["score"] >= threshold
    decision = "allowed" if allowed else "banned"
    reason = limit_words(evaluation["reason"], 50)[:250]
    return {
        **evaluation,
        "decision": decision,
        "reason": reason,
        "threshold": threshold,
        "model": MODEL,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
