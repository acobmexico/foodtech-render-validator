import ipaddress
import json
import os
import re
import socket
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urldefrag, urlunparse

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from pydantic import BaseModel, Field

from analysis_cache import get_cached, put_cached


MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
SEARCH_MODEL = os.getenv("OPENAI_SEARCH_MODEL", MODEL)
REQUEST_TIMEOUT = int(os.getenv("WEB_REQUEST_TIMEOUT", "35"))
MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 5
MAX_CHARS_MAIN = 18_000
MAX_CHARS_CORPORATE = 12_000
MIN_USEFUL_CHARS = 180
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
13. CAPACITACIÓN ESPECIALIZADA: una empresa dedicada principalmente a capacitación,
    consultoría o formación solamente califica cuando las páginas demuestran con
    claridad que su especialidad es agroalimentaria, industria alimentaria,
    operación gastronómica profesional, inocuidad, calidad, procesamiento,
    ingredientes o empaque. La capacitación genérica, empresarial o de otras
    industrias NO_CALIFICA. Una especialización alimentaria clara recibe al menos
    65 puntos y se clasifica CAPACITACION_ESPECIALIZADA.
14. REPRESENTACIÓN COMERCIAL INTERNACIONAL: embajadas, consulados, oficinas de
    comercio exterior, agencias de promoción internacional y representaciones
    comerciales gubernamentales extranjeras califican cuando su función de enlace,
    promoción o desarrollo comercial internacional esté demostrada. Clasifícalas
    REPRESENTACION_COMERCIAL_INTERNACIONAL y asigna al menos 65 puntos.
15. SERVICIO ESPECIALIZADO: empresas de certificación, auditoría, laboratorio,
    estándares, inocuidad, calidad, cumplimiento o consultoría técnica califican
    si las páginas demuestran aplicación directa en alimentos y bebidas. La
    calidad genérica para cualquier industria no basta. Clasifica estos casos
    SERVICIO_ESPECIALIZADO y asigna al menos 65 puntos.
16. Fabricantes o proveedores de envases, empaques, embalajes, contenedores,
    sistemas de llenado, embotellado o maquinaria de empaque para alimentos y
    bebidas califican. No exijas que el sitio use literalmente la palabra
    FOODTECH si la aplicación alimentaria está demostrada.
17. Una cadena de restaurantes o una operación gastronómica comercial califica
    como COMPRADOR porque naturalmente adquiere alimentos, bebidas, ingredientes,
    equipos, empaque y soluciones de operación.
18. Universidades, asociaciones, consultores, gobierno, despachos, escuelas y
    servicios generales reciben calificación baja salvo una actividad sectorial
    expresamente admitida en estas reglas.
19. Analiza el contenido en cualquier idioma. Comprende y traduce internamente
    inglés, francés, alemán, portugués, italiano y cualquier otro idioma presente.
    Nunca reduzcas el puntaje por no estar en español.
20. Si no existe evidencia suficiente del giro, asigna una calificación baja o 0.
21. Si decides NO_CALIFICA con evidencia disponible, explica concretamente cuál
    es la actividad encontrada y por qué está fuera de los criterios FOODTECH.
    Evita frases vagas como “no cumple” o “no hay relación” sin indicar la causa.
22. Escala estricta: 100 inequívoco; 80-99 relación sólida; 60-79 relevante pero
    parcial; 30-59 secundaria; 1-29 débil; 0 sin evidencia suficiente.
23. Para un medio FOODTECH inequívoco usa normalmente 70-90 puntos. Reserva
    65-69 para especialización válida pero con evidencia limitada.
24. La razón debe estar en español y contener como máximo 50 palabras.
25. No menciones puestos ni sugieras que faltó conocerlos.

EJEMPLOS DE DECISIÓN:
- Revista o portal con directorio, noticias y capacitación para profesionales de
  alimentos, ingredientes o procesamiento: MEDIO_ESPECIALIZADO y aprobado.
- Revista automotriz sin una división alimentaria demostrada: NO_CALIFICA.
- Portal general de noticias que ocasionalmente publica sobre comida: NO_CALIFICA.
- Empresa de capacitación agroalimentaria claramente especializada: aprobada.
- Academia de capacitación empresarial genérica: NO_CALIFICA.
- Embajada u oficina comercial internacional demostrada: aprobada.
- Empresa de normas y calidad aplicada a alimentos: aprobada.
- Cadena de restaurantes: COMPRADOR y aprobada.
""".strip()


class CompanyEvaluation(BaseModel):
    score: int = Field(ge=0, le=100)
    type: Literal[
        "COMPRADOR", "DISTRIBUIDOR", "AMBOS", "MEDIO_ESPECIALIZADO",
        "CAPACITACION_ESPECIALIZADA", "REPRESENTACION_COMERCIAL_INTERNACIONAL",
        "SERVICIO_ESPECIALIZADO",
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
    url = re.sub(r"\s+", "", url)
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url
    url = unwrap_search_redirect(url)
    parsed = urlparse(url)
    host = (parsed.hostname or "").replace(",", ".")
    host = re.sub(r"\.con$", ".com", host, flags=re.I)
    host = re.sub(r"\.c0m$", ".com", host, flags=re.I)
    if host and host != parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        url = urlunparse(parsed._replace(netloc=host + port))
        parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("La URL no es válida.")
    if parsed.username or parsed.password:
        raise ValueError("La URL no puede incluir credenciales.")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("El puerto de la URL no está permitido.")
    return url


def candidate_urls(raw_url: str) -> list[str]:
    """Genera hasta tres variantes razonables sin inventar un dominio distinto."""
    primary = normalize_url(raw_url)
    parsed = urlparse(primary)
    host = parsed.hostname or ""
    candidates = [primary]
    toggled_host = host[4:] if host.startswith("www.") else "www." + host
    toggled = urlunparse(parsed._replace(netloc=toggled_host))
    candidates.append(toggled)
    alternate_scheme = "http" if parsed.scheme == "https" else "https"
    candidates.append(urlunparse(parsed._replace(scheme=alternate_scheme)))
    return list(dict.fromkeys(candidates))[:3]


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


def download_via_reader(url: str) -> tuple[str, str]:
    target = normalize_url(url)
    reject_private_destination(target)
    reader_url = "https://r.jina.ai/" + target
    response = requests.get(
        reader_url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
        timeout=(10, max(45, REQUEST_TIMEOUT)),
    )
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("La lectura alternativa supera el tamaño máximo permitido.")
    text = re.sub(r"\s+", " ", response.text or "").strip()
    if len(text) < MIN_USEFUL_CHARS:
        raise ValueError("La lectura alternativa no devolvió contenido suficiente.")
    return target, text[:MAX_CHARS_MAIN]


def _parse_search_json(text: str) -> dict:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        raise ValueError("La búsqueda web no devolvió JSON interpretable.")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("La búsqueda web no devolvió un objeto.")
    return payload


def _response_source_urls(response) -> list[str]:
    try:
        data = response.model_dump()
    except Exception:
        return []
    urls = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"url", "source_url"} and isinstance(item, str):
                    if item.startswith(("http://", "https://")):
                        urls.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data.get("output", []))
    return list(dict.fromkeys(urls))


def search_company_web(company_name: str, supplied_url: str) -> dict:
    prompt = f"""
Busca evidencia pública verificable sobre esta empresa y su sitio oficial.
EMPRESA: {company_name}
URL DECLARADA: {supplied_url}

Determina su actividad actual usando como máximo una búsqueda web. Prioriza el
sitio oficial y fuentes corporativas o institucionales. Puedes leer cualquier
idioma. No confundas empresas homónimas. Devuelve únicamente JSON:
{{
  "conclusive": true o false,
  "official_url": "URL oficial o cadena vacía",
  "business_evidence": "resumen factual detallado de la actividad",
  "sources": ["URL 1", "URL 2"]
}}
Marca conclusive=true solamente si la identidad coincide y hay evidencia útil.
""".strip()
    last_error = None
    for tool_name in ("web_search", "web_search_preview"):
        try:
            response = get_openai_client().responses.create(
                model=SEARCH_MODEL,
                tools=[{"type": tool_name}],
                input=prompt,
            )
            payload = _parse_search_json(response.output_text)
            sources = [str(item) for item in payload.get("sources", []) if item]
            sources = list(dict.fromkeys(sources + _response_source_urls(response)))
            evidence = str(payload.get("business_evidence") or "").strip()
            official_url = str(payload.get("official_url") or "").strip()
            conclusive = (
                bool(payload.get("conclusive"))
                and len(evidence) >= 80
                and bool(sources)
            )
            return {
                "conclusive": conclusive,
                "official_url": official_url,
                "evidence": evidence,
                "sources": sources[:5],
            }
        except Exception as exc:
            last_error = exc
            if "web_search" not in str(exc).casefold() and "tool" not in str(exc).casefold():
                break
    raise RuntimeError(f"La búsqueda web falló: {type(last_error).__name__}: {last_error}")


def _direct_information(final_url: str, main_html: str) -> dict:
    main_text, main_chars = extract_visible_text(main_html, MAX_CHARS_MAIN)
    corporate_candidate = find_corporate_page(main_html, final_url)
    corporate_url, corporate_text, corporate_chars, corporate_error = "", "", 0, ""
    if corporate_candidate:
        try:
            found_url, found_html = download_html(corporate_candidate["url"])
            if not same_domain(final_url, found_url):
                raise ValueError("La página corporativa redirigió a otro dominio.")
            corporate_text, corporate_chars = extract_visible_text(found_html, MAX_CHARS_CORPORATE)
            corporate_url = found_url
        except Exception as exc:
            corporate_error = f"{type(exc).__name__}: {exc}"
    return {
        "main_url": final_url, "main_text": main_text, "main_chars": main_chars,
        "corporate_url": corporate_url, "corporate_text": corporate_text,
        "corporate_chars": corporate_chars, "corporate_error": corporate_error,
        "retrieval_method": "direct", "search_sources": [], "conclusive": True,
    }


def collect_company_information(url: str, company_name: str) -> dict:
    attempts = []
    candidates = candidate_urls(url)

    # Camino 1: acceso directo, incluyendo hasta tres variantes técnicas de la URL.
    for candidate in candidates:
        try:
            final_url, main_html = download_html(candidate)
            information = _direct_information(final_url, main_html)
            useful_chars = information["main_chars"] + information["corporate_chars"]
            if useful_chars >= MIN_USEFUL_CHARS:
                information["retrieval_attempts"] = attempts + [f"direct:{candidate}:ok"]
                return information
            attempts.append(f"direct:{candidate}:contenido insuficiente ({useful_chars} caracteres)")
        except Exception as exc:
            attempts.append(f"direct:{candidate}:{type(exc).__name__}: {exc}")

    # Camino 2: lector alternativo para 403, páginas lentas o contenido JavaScript.
    try:
        reader_target, reader_text = download_via_reader(candidates[0])
        return {
            "main_url": reader_target,
            "main_text": f"CONTENIDO RECUPERADO MEDIANTE LECTOR ALTERNATIVO:\n{reader_text}",
            "main_chars": len(reader_text), "corporate_url": "", "corporate_text": "",
            "corporate_chars": 0, "corporate_error": "", "retrieval_method": "reader",
            "retrieval_attempts": attempts + ["reader:ok"], "search_sources": [],
            "conclusive": True,
        }
    except Exception as exc:
        attempts.append(f"reader:{type(exc).__name__}: {exc}")

    # Camino 3: búsqueda web asistida. No se raspan páginas de resultados de Google.
    try:
        search = search_company_web(company_name, candidates[0])
        if search["conclusive"]:
            return {
                "main_url": search["official_url"] or candidates[0],
                "main_text": "EVIDENCIA RECUPERADA MEDIANTE BÚSQUEDA WEB:\n" + search["evidence"],
                "main_chars": len(search["evidence"]), "corporate_url": "",
                "corporate_text": "", "corporate_chars": 0, "corporate_error": "",
                "retrieval_method": "web_search",
                "retrieval_attempts": attempts + ["web_search:ok"],
                "search_sources": search["sources"], "conclusive": True,
            }
        attempts.append("web_search:sin evidencia concluyente")
    except Exception as exc:
        attempts.append(f"web_search:{type(exc).__name__}: {exc}")

    raise ValueError(" | ".join(attempts)[-3000:])


def no_evidence_evaluation(reason: str, exact_reason: str = "") -> dict:
    concise = limit_words(
        exact_reason or (
            "No fue posible obtener evidencia verificable después de tres caminos: "
            f"acceso directo, lector alternativo y búsqueda web. Detalle: {reason}"
        ),
        50,
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
        "retrieval_method": "none",
        "retrieval_attempts": reason,
        "search_sources": [],
        "conclusive": False,
        "cache_hit": False,
    }


def evaluate_record(record: dict, threshold: int) -> dict:
    page = str(record.get("PaginaWeb") or "").strip()
    company_name = str(record.get("RazonSocial") or "").strip()
    if not page:
        evaluation = no_evidence_evaluation(
            "La empresa no proporcionó página web.",
            exact_reason="NO SE PROPORCIONO WEBSITE",
        )
        evaluation.update({
            "decision": "banned", "reason": "NO SE PROPORCIONO WEBSITE",
            "threshold": threshold, "model": MODEL,
            "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        return evaluation

    cached = get_cached(company_name, page)
    if cached:
        cached["decision"] = "allowed" if int(cached["score"]) >= threshold else "banned"
        cached["threshold"] = threshold
        cached["evaluated_at_utc"] = datetime.now(timezone.utc).isoformat()
        return cached

    try:
        information = collect_company_information(page, company_name)
    except Exception as exc:
        evaluation = no_evidence_evaluation(f"{type(exc).__name__}: {exc}")
    else:
        corporate_block = (
            f"PÁGINA CORPORATIVA ADICIONAL:\nURL: {information['corporate_url']}\n"
            f"{information['corporate_text']}"
            if information["corporate_url"]
            else "No se obtuvo una página corporativa adicional."
        )
        sources_block = "\n".join(information.get("search_sources") or [])
        user_input = f"""
RAZÓN SOCIAL DECLARADA: {company_name}
URL PROPORCIONADA: {information['main_url']}
MÉTODO DE OBTENCIÓN: {information['retrieval_method']}
FUENTES DE BÚSQUEDA: {sources_block or 'No aplican; contenido obtenido del sitio.'}

CONTENIDO DE LA PÁGINA PROPORCIONADA:
---
{information['main_text']}
---

{corporate_block}

Evalúa exclusivamente el giro y la actividad actual demostrados por estas páginas.
No consideres Cargo ni Area aunque existan en el registro.
El contenido puede estar en cualquier idioma; interprétalo sin penalizar el idioma.
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
            "retrieval_method": information["retrieval_method"],
            "retrieval_attempts": " | ".join(information.get("retrieval_attempts") or []),
            "search_sources": information.get("search_sources") or [],
            "conclusive": bool(information.get("conclusive")),
            "cache_hit": False,
        }

    allowed = evaluation["score"] >= threshold
    decision = "allowed" if allowed else "banned"
    reason = limit_words(evaluation["reason"], 50)[:250]
    final_evaluation = {
        **evaluation,
        "decision": decision,
        "reason": reason,
        "threshold": threshold,
        "model": MODEL,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if final_evaluation.get("conclusive"):
        put_cached(company_name, page, final_evaluation)
    return final_evaluation
