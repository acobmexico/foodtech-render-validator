import json
import time
from dataclasses import dataclass

import requests


class FoodtechAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class FoodtechAPIClient:
    base_url: str
    token: str
    timeout_seconds: int = 45

    def __post_init__(self):
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        if not self.base_url:
            raise ValueError("FOODTECH_API_BASE_URL no está configurada.")
        if not self.token:
            raise ValueError("FOODTECH_API_TOKEN no está configurado.")

    @staticmethod
    def _unwrap_response(response: requests.Response) -> dict:
        try:
            outer = response.json()
        except ValueError as exc:
            preview = (response.text or "")[:400]
            raise FoodtechAPIError(
                f"El endpoint respondió HTTP {response.status_code} sin JSON: {preview}"
            ) from exc

        payload = outer.get("d") if isinstance(outer, dict) and "d" in outer else outer
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise FoodtechAPIError(
                    "El campo 'd' no contiene un objeto JSON válido."
                ) from exc
        if not isinstance(payload, dict):
            raise FoodtechAPIError("La respuesta útil del endpoint no es un objeto JSON.")
        if payload.get("success") is False:
            raise FoodtechAPIError(payload.get("error") or "El endpoint reportó un error.")
        if response.status_code >= 400:
            raise FoodtechAPIError(
                f"El endpoint respondió HTTP {response.status_code}: {payload}"
            )
        return payload

    def _post(self, method_name: str, body: dict, retry_safe: bool) -> dict:
        url = f"{self.base_url}/{method_name}"
        attempts = 3 if retry_safe else 1
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(
                    url,
                    json=body,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    timeout=(10, self.timeout_seconds),
                )
                return self._unwrap_response(response)
            except (requests.RequestException, FoodtechAPIError) as exc:
                last_error = exc
                if attempt == attempts:
                    break
                time.sleep(2 ** attempt)
        raise FoodtechAPIError(str(last_error)) from last_error

    def consult_pending(self, max_records: int = 10, after_id: int = 0) -> dict:
        body = {"token": self.token, "maxRecords": max_records}
        if after_id:
            body["afterId"] = after_id
        return self._post("consultPendingRecords", body, retry_safe=True)

    def save_validation(
        self,
        enrollment_code: str,
        visitor_id: str,
        decision: str,
        reason: str,
    ) -> dict:
        body = {
            "token": self.token,
            "AprobacionIA": decision[:250],
            "RazonamientoIA": reason[:250],
            "estatusAcceso": decision,
        }
        if enrollment_code:
            body["enrollmentCode"] = enrollment_code
        elif visitor_id:
            body["IDVisitante"] = visitor_id
        else:
            raise FoodtechAPIError(
                "El registro no tiene enrollmentCode ni IDVisitante."
            )

        # No se reintenta automáticamente: si la conexión se pierde después de que
        # ASP.NET guardó, un reintento produciría un snapshot histórico duplicado.
        return self._post("saveAIValidation", body, retry_safe=False)
