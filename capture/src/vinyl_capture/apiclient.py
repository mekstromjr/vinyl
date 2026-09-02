"""Cluster API client — the worker's only source of truth."""

from __future__ import annotations

import httpx


class ApiError(RuntimeError):
    pass


class VinylApi:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"X-API-Key": api_key},
        )

    def _req(self, method: str, path: str, **kwargs):
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(f"cluster unreachable: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ApiError(f"{resp.status_code}: {detail}")
        return resp.json() if resp.content else None

    def sides(self, accession_id: str) -> list[dict]:
        return self._req("GET", f"/api/accessions/{accession_id}/sides")

    def accession(self, accession_id: str) -> dict:
        return self._req("GET", f"/api/accessions/{accession_id}")

    def start_capture(self, side_id: str) -> dict:
        return self._req("POST", "/api/captures", json={"side_id": side_id})

    def finish_capture(self, capture_id: str, **fields) -> dict:
        return self._req("PATCH", f"/api/captures/{capture_id}", json=fields)

    def mark_uploaded(self, capture_id: str, bucket: str, key: str) -> dict:
        return self._req(
            "POST", f"/api/captures/{capture_id}/uploaded",
            json={"s3_bucket": bucket, "s3_key": key},
        )

    def report_upload_error(self, capture_id: str, error: str) -> None:
        self._req("POST", f"/api/captures/{capture_id}/upload-error", json={"error": error[:500]})

    def clear_expected_runtime(self, side_id: str) -> None:
        accession_id = side_id.rsplit("-", 2)[0]
        self._req(
            "PATCH", f"/api/accessions/{accession_id}/sides/{side_id}",
            json={"clear_expected_runtime": True},
        )

    def capture(self, capture_id: str) -> dict:
        return self._req("GET", f"/api/captures/{capture_id}")

    # ------------------------------------------------------------- stations --
    def register_station(self, station_id: str, **fields) -> dict:
        """PUT, not POST: the agent calls this on every start with the same id,
        so a restart must update the row rather than conflict."""
        return self._req("PUT", f"/api/stations/{station_id}", json=fields)

    def station_heartbeat(self, station_id: str, **fields) -> dict:
        return self._req("POST", f"/api/stations/{station_id}/heartbeat", json=fields)
