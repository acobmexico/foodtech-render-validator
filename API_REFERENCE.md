# API — foodtech2026_aiValidator

Servicio de validación por IA de los pases de **FOODTECH 2026**. Expone dos
operaciones: una para **listar** los pases que faltan por validar y otra para
**guardar** el dictamen que produzca la IA.

---

## 1. Reglas de transporte (leer primero)

Son *Page Methods* de ASP.NET Web Forms. Para llamarlos correctamente:

| Regla | Valor |
|---|---|
| Método HTTP | **POST** (cualquier otro devuelve error) |
| URL | `{BASE_URL}/<NombreDelMetodo>` |
| Header obligatorio | `Content-Type: application/json; charset=utf-8` |
| Body | Objeto JSON |
| Autenticación | Campo `token` en el body (secreto compartido) |

`{BASE_URL}` = `http://localhost/acobV/printTotem/foodtech2026_aiValidator.aspx`
(reemplazar por el host real en producción).

### ⚠️ Envoltura de la respuesta `{"d": ...}`

ASP.NET envuelve el retorno en un campo `d`, y **el contenido de `d` es un
string que a su vez contiene el JSON real**. Hay que parsear dos veces.

Respuesta tal cual viaja por el cable:

```json
{"d":"{\"success\":true,\"recordsFound\":10, ... }"}
```

El consumidor debe: (1) parsear el body → obtener `d`; (2) parsear `d` como
JSON → obtener el objeto útil. Ejemplo en pseudocódigo:

```
raw     = json.parse(httpBody)      // { "d": "..." }
payload = json.parse(raw["d"])      // objeto real
```

---

## 2. Endpoint: `consultPendingRecords`

Devuelve los pases pendientes de validar. Un pase entra en la lista solo si
cumple **todo**:

- `TipoRegistro = 'visitante'`
- `estatusAcceso = 'unknown'`
- `AprobacionIA_Fecha` está vacío (aún no se captura IA)
- no está borrado

Resultado ordenado por `id` ascendente.

### Request

| Campo | Tipo | Req. | Default | Descripción |
|---|---|---|---|---|
| `token` | string | ✔ | — | Secreto compartido |
| `maxRecords` | int | ✘ | 100 | Tope de filas (máx. 1000) |
| `afterId` | int | ✘ | 0 | Trae solo `id > afterId`. Para paginar, mandar el `lastId` del lote previo |

**Ejemplo (primer lote):**

```json
POST {BASE_URL}/consultPendingRecords
Content-Type: application/json; charset=utf-8

{ "token": "TU_TOKEN", "maxRecords": 10 }
```

**Ejemplo (siguiente lote):** usar el `lastId` recibido antes.

```json
{ "token": "TU_TOKEN", "maxRecords": 10, "afterId": 4487 }
```

### Respuesta (ya desenvuelta de `d`)

```json
{
  "success": true,
  "recordsFound": 2,
  "maxRecords": 10,
  "lastId": 4473,
  "hasMore": false,
  "data": [
    {
      "id": 4471,
      "enrollmentCode": "1090997",
      "IDVisitante": "1090997",
      "Nombre": "Mario",
      "ApellidoPaterno": "Saavedra",
      "RazonSocial": "Mykos-LAB",
      "Cargo": "Director",
      "Area": "Investigación",
      "Email": "saavedramsa@gmail.com",
      "PaginaWeb": "https://www.colpos.mx",
      "estatusAcceso": "unknown"
    },
    {
      "id": 4473,
      "enrollmentCode": "1180158",
      "IDVisitante": "1180158",
      "Nombre": "Germán",
      "ApellidoPaterno": "Morales",
      "RazonSocial": "La Primavera",
      "Cargo": "Propietario",
      "Area": "Compras",
      "Email": "laprimaveravinosylicores@gmail.com",
      "PaginaWeb": "https://Laprimaveravinosylicores.com",
      "estatusAcceso": "unknown"
    }
  ],
  "timestamp": "2026-09-22 06:00"
}
```

**Paginación:** repetir la llamada con `afterId = lastId` mientras
`hasMore = true`. Cuando `hasMore = false` ya no quedan pendientes.

**Nota de calidad de datos:** `PaginaWeb` puede venir vacía, inválida
(`sadfsadf.com`) o apuntar a redes sociales (Instagram, etc.). La IA debe
tolerar esos casos sin fallar y reflejarlo en su razonamiento.

---

## 3. Endpoint: `saveAIValidation`

Guarda el dictamen de la IA sobre **un** pase. Localiza la fila por
`enrollmentCode` **o** `IDVisitante` (si vienen ambos, gana `enrollmentCode`).
Sella `AprobacionIA_Fecha` con la hora del servidor. Vuelve a llamarlo sobre el
mismo pase **sobrescribe** el dictamen (y genera un nuevo snapshot de
historial).

### Request

| Campo | Tipo | Req. | Descripción |
|---|---|---|---|
| `token` | string | ✔ | Secreto compartido |
| `enrollmentCode` | string | ✔* | Clave del pase. *Requerido si no se manda `IDVisitante` |
| `IDVisitante` | string | ✔* | Clave externa. *Requerido si no se manda `enrollmentCode`. Alias aceptados: `externalCode`, `external_code`, `external` |
| `AprobacionIA` | string(250) | ✔ | Dictamen de la IA (p.ej. `allowed`, `banned`, `revisar`). Se trunca a 250 |
| `RazonamientoIA` | string(250) | ✘ | Motivo de la decisión. Se trunca a 250 |
| `estatusAcceso` | string | ✘ | Si se envía, actualiza el acceso en la misma operación. Valores válidos: `unknown`, `allowed`, `banned` |

> **Recomendación:** manda `estatusAcceso` junto con el dictamen (típicamente
> `allowed` o `banned`) para sacar al pase de `unknown`. Aunque no lo mandes, el
> pase deja de aparecer en `consultPendingRecords` porque ya quedó sellada la
> fecha de IA.

**Ejemplo:**

```json
POST {BASE_URL}/saveAIValidation
Content-Type: application/json; charset=utf-8

{
  "token": "TU_TOKEN",
  "enrollmentCode": "1090997",
  "AprobacionIA": "allowed",
  "RazonamientoIA": "colpos.mx es dominio institucional válido; perfil consistente con el evento.",
  "estatusAcceso": "allowed"
}
```

### Respuesta (ya desenvuelta de `d`)

```json
{
  "success": true,
  "message": "Validacion de IA guardada correctamente",
  "id": 4471,
  "enrollmentCode": "1090997",
  "IDVisitante": "1090997",
  "estatusAcceso": "allowed",
  "aprobacionTruncada": false,
  "razonamientoTruncado": false,
  "changeId": 4441,
  "timestamp": "2026-09-22 06:09"
}
```

| Campo | Significado |
|---|---|
| `estatusAcceso` | El nuevo valor, o `"(sin cambio)"` si no se envió |
| `aprobacionTruncada` | `true` si `AprobacionIA` superaba 250 y se recortó |
| `razonamientoTruncado` | `true` si `RazonamientoIA` superaba 250 y se recortó |
| `changeId` | Id del snapshot generado en el historial (0 si falló el snapshot) |

---

## 4. Respuestas de error

Todos los errores comparten la forma `{ "success": false, "error": "...", "timestamp": "..." }`.
(Se entregan igualmente envueltas en `d`, y el HTTP puede ser 200 o 500 según el caso.)

| Situación | `error` |
|---|---|
| Token no coincide | `Token invalido` |
| Falta `enrollmentCode` e `IDVisitante` | `Se requiere 'enrollmentCode' o 'IDVisitante'` |
| Falta `AprobacionIA` | `Parametro 'AprobacionIA' es requerido` |
| `estatusAcceso` fuera de dominio | `estatusAcceso invalido. Valores permitidos: unknown, allowed, banned.` |
| Pase no existe / borrado | `Registro no encontrado` (incluye `enrollmentCode` e `IDVisitante`) |
| Body vacío o mal formado | `Body JSON requerido` / `JSON invalido: ...` |
| No es POST | `Metodo HTTP no permitido...` (incluye `receivedMethod`) |

La IA debe tratar cualquier respuesta con `success: false` como fallo de esa
operación y **no** asumir que el pase fue validado.

---

## 5. Flujo recomendado para el agente de IA

1. `consultPendingRecords` con `maxRecords` y `afterId` para recorrer todos los pendientes por lotes.
2. Por cada pase: investigar con `RazonSocial`, `Cargo`, `Area`, `Email` y `PaginaWeb`; decidir.
3. `saveAIValidation` con `enrollmentCode`, `AprobacionIA`, `RazonamientoIA` y `estatusAcceso` (`allowed`/`banned`).
4. Repetir el paso 1 con el `lastId` recibido hasta que `hasMore = false`.

### Ejemplo con `curl`

```bash
# Listar pendientes
curl -X POST "http://localhost/acobV/printTotem/foodtech2026_aiValidator.aspx/consultPendingRecords" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"token":"TU_TOKEN","maxRecords":10}'

# Guardar dictamen
curl -X POST "http://localhost/acobV/printTotem/foodtech2026_aiValidator.aspx/saveAIValidation" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"token":"TU_TOKEN","enrollmentCode":"1090997","AprobacionIA":"allowed","RazonamientoIA":"dominio institucional valido","estatusAcceso":"allowed"}'
```
