# FOODTECH 2026 AI Validator para Render

Servicio que consulta hasta diez pases pendientes, evalúa en paralelo el giro de
cada empresa y guarda individualmente el dictamen mediante `saveAIValidation`.
No conserva resultados localmente.

## Operación

- `POST /api/run-batch`: consulta pendientes y procesa el lote.
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
- Sitio vacío, inválido, red social o inaccesible: puntaje 0 por falta de evidencia
  verificable y `banned`.
- Error técnico de OpenAI: no se llama `saveAIValidation`.
- Error al guardar: se devuelve `save_error` y nunca se asume que quedó validado.
- `saveAIValidation` no se reintenta automáticamente para evitar snapshots
  históricos duplicados si se pierde la respuesta después de guardar.

## Paralelismo

El lote conserva el orden recibido en la respuesta, aunque los casos se procesan en
paralelo. Para una instancia pequeña, inicie con cinco. Si OpenAI devuelve errores
429 o Render alcanza memoria/CPU alta, reduzca `MAX_PARALLEL_CASES` a tres.
