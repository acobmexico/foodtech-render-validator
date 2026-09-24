# FOODTECH 2026 AI Validator para Render

Servicio que consulta hasta diez pases pendientes, evalúa en paralelo el giro de
cada empresa y guarda individualmente el dictamen mediante `saveAIValidation`.
Mantiene un registro incremental, un Excel descargable y una caché de análisis
concluyentes.

## Operación

- `POST /api/run-batch`: consulta pendientes y procesa el lote.
- `POST /api/run-all`: inicia todos los pendientes en segundo plano, en lotes de 10.
- `GET /api/run-all/status`: devuelve avance, totales y errores recientes.
- `POST /api/run-all/stop`: se detiene después de terminar el lote actual.
- `GET /api/results.xlsx`: genera y descarga el Excel acumulado.
- `POST /api/process-package`: procesa un arreglo recibido de 1 a 10 registros.
- `GET /health`: comprueba configuración y estado.
- `GET /`: interfaz privada para una prueba manual.

El servicio utiliza un solo proceso de Uvicorn y cinco hilos de análisis de forma
predeterminada. Un bloqueo impide ejecutar dos lotes simultáneos en la misma
instancia. No habilite más de una instancia de Render sin implementar antes un
bloqueo distribuido, pues dos instancias podrían consultar los mismos pendientes.

## Variables de Render

| Variable | Ejemplo o descripción |
|---|---|
| `OPENAI_API_KEY` | API key de OpenAI |
| `APP_ACCESS_KEY` | Contraseña para invocar este servicio |
| `FOODTECH_API_BASE_URL` | URL pública hasta `foodtech2026_aiValidator.aspx`, sin nombre de método |
| `FOODTECH_API_TOKEN` | Token compartido del API FOODTECH |
| `SCORE_THRESHOLD` | `65` |
| `MAX_PARALLEL_CASES` | Comenzar con `5`; máximo permitido por el código: `10` |
| `OPENAI_MODEL` | `gpt-5-mini` |
| `OPENAI_SEARCH_MODEL` | Modelo de la tercera ruta de recuperación web |
| `WEB_REQUEST_TIMEOUT` | `35` segundos por solicitud directa |
| `AUTO_RUN_ON_START` | `false`: inicio con botón; `true`: reanuda automáticamente al iniciar Render |
| `RESULTS_CSV_PATH` | Registro incremental; por defecto `/tmp/FOODTECH_VALIDACION_RESULTADOS.csv` |
| `RESULTS_XLSX_PATH` | Excel descargable; por defecto `/tmp/FOODTECH_VALIDACION_RESULTADOS.xlsx` |
| `ANALYSIS_CACHE_PATH` | Caché SQLite de evaluaciones concluyentes |

## Recuperación del sitio y reglas internacionales

El sistema utiliza como máximo tres caminos antes de declarar que no obtuvo
evidencia: (1) acceso directo con correcciones conservadoras y variantes
`www`/protocolo; (2) lector alternativo para bloqueos 403, sitios lentos o
contenido dinámico; y (3) búsqueda web asistida mediante OpenAI. Nunca raspa
páginas de resultados de Google, por lo que evita CAPTCHA y rate-limiting.

Errores evidentes como `www.bactersanmx,con` se corrigen a
`www.bactersanmx.com`. La evaluación comprende contenido en cualquier idioma.
Si no se proporcionó sitio, el razonamiento guardado es exactamente
`NO SE PROPORCIONO WEBSITE` y no se intenta descubrir uno por nombre.

Con evidencia clara también pueden aprobarse medios FOODTECH, capacitación
agroalimentaria especializada, embajadas y representaciones comerciales
internacionales, servicios de calidad o inocuidad alimentaria, empresas de
empaque y cadenas de restaurantes.

## Caché concluyente

Las evaluaciones con evidencia suficiente se guardan por dominio y empresa en
SQLite. Una coincidencia posterior reutiliza la evaluación y evita el crawling y
la llamada de análisis. Los casos sin sitio, inaccesibles o sin evidencia nunca se
guardan en caché y pueden reintentarse.

## Archivo Excel

Cada intento se agrega inmediatamente al registro incremental. El botón
**Descargar Excel** genera `FOODTECH_VALIDACION_RESULTADOS.xlsx` con empresa,
identificadores, sitio, estado, puntaje, tipo, decisión, giro, evidencia,
razonamiento, URLs analizadas, errores, tiempo y modelo. El libro incluye filtros,
encabezado fijo, ajuste de texto y limpieza de caracteres ilegales.

`/tmp` es almacenamiento temporal de Render. Para conservar el historial después
de reinicios, monte un Persistent Disk en `/var/data` y cambie las dos variables
a `/var/data/FOODTECH_VALIDACION_RESULTADOS.csv` y
`/var/data/FOODTECH_VALIDACION_RESULTADOS.xlsx`. Configure también
`ANALYSIS_CACHE_PATH=/var/data/foodtech_analysis_cache.sqlite3`.

## Procesar los 5,000 casos

Abra `/`, capture `APP_ACCESS_KEY` y pulse **Procesar todos los pendientes** una
sola vez. La página puede cerrarse: el proceso continúa en Render. Cada dictamen
se guarda de inmediato; si un caso falla, permanece pendiente y aparece entre
los errores recientes. La corrida avanza el cursor para que un error no provoque
un ciclo infinito.

Para reanudar automáticamente después de un reinicio o despliegue, cambie
`AUTO_RUN_ON_START` a `true` en Render. La API solamente devuelve pendientes,
por lo que no repite los casos que ya se guardaron correctamente.

La URL `localhost` de la documentación no funciona desde Render. Debe configurarse
el host HTTPS público o una dirección que Render pueda alcanzar por Internet.

## Instalación sin Terminal

1. Cree en GitHub un repositorio privado vacío llamado `foodtech-render-validator`.
2. Descomprima el ZIP y suba el contenido de esta carpeta directamente a la raíz
   del repositorio mediante **Add file > Upload files**.
3. En Render seleccione **New > Blueprint**, conecte el repositorio y confirme que
   detectó `render.yaml`.
4. Capture las cuatro variables secretas solicitadas y aplique el Blueprint.
5. Cuando el servicio indique **Live**, abra `/health` y confirme que las dos
   configuraciones aparecen como `true`.
6. Abra `/`, escriba `APP_ACCESS_KEY` y procese primero un lote de uno.
7. Revise el resultado y después pruebe un lote de diez.

## Contrato de entrada de run batch

```json
{
  "max_records": 10,
  "after_id": 0
}
```

Header obligatorio:

```text
X-API-Key: APP_ACCESS_KEY
```

Para recibir directamente un paquete preparado por otra aplicación se utiliza
`POST /api/process-package` con un máximo de diez elementos:

```json
{
  "records": [
    {
      "id": 4471,
      "enrollmentCode": "1090997",
      "IDVisitante": "1090997",
      "RazonSocial": "Empresa ejemplo",
      "PaginaWeb": "https://empresa.example"
    }
  ]
}
```

El razonamiento guardado incluye puntaje, clasificación y motivo, por ejemplo:

```text
87/100; COMPRADOR; La página demuestra una operación directa de procesamiento...
```

## Decisiones y fallos

- Puntaje mayor o igual a `SCORE_THRESHOLD`: `allowed`.
- Puntaje menor: `banned`.
- Un medio editorial especializado y demostrablemente enfocado en FOODTECH se
  clasifica `MEDIO_ESPECIALIZADO` y obtiene al menos 65 puntos. Medios
  generalistas o de otros sectores no califican por el solo hecho de ser medios.
- Si los tres caminos de recuperación terminan sin evidencia verificable, el
  registro recibe puntaje 0 y queda fuera de la caché para permitir reintentos.
- El crawler nunca raspa directamente resultados de Google, Bing o Yahoo. La
  tercera ruta utiliza la herramienta de búsqueda web de OpenAI y evita CAPTCHA
  y bloqueos contra la IP de Render.
- Error técnico de OpenAI: no se llama `saveAIValidation`.
- Error al guardar: se devuelve `save_error` y nunca se asume que quedó validado.
- `saveAIValidation` no se reintenta automáticamente para evitar snapshots
  históricos duplicados si se pierde la respuesta después de guardar.

## Paralelismo

El lote conserva el orden recibido en la respuesta, aunque los casos se procesan en
paralelo. Para una instancia pequeña, inicie con cinco. Si OpenAI devuelve errores
429 o Render alcanza memoria/CPU alta, reduzca `MAX_PARALLEL_CASES` a tres.
