"""Spawn QC Jobs via the Kubernetes API — event-driven one-shot Jobs, no crons.

Talks to kubernetes.default.svc directly with the pod's ServiceAccount token
(one POST; the official client would be a heavyweight dependency for that).
Spawn failures are logged, never raised into the upload path: a capture whose
QC Job failed to spawn stays visibly qc_pending and the manual re-run endpoint
covers it.
"""

from __future__ import annotations

import logging
import os
import uuid

import httpx

log = logging.getLogger(__name__)

_SA = "/var/run/secrets/kubernetes.io/serviceaccount"


def qc_job_manifest(capture_id: uuid.UUID, image: str, namespace: str) -> dict:
    """Pure manifest builder (unit-testable without a cluster)."""
    name = f"vinyl-qc-{str(capture_id)[:8]}-{os.urandom(2).hex()}"
    secret_env = [
        {"name": "VINYL_DB_PASSWORD",
         "valueFrom": {"secretKeyRef": {"name": "vinyl-secrets", "key": "db-password"}}},
        {"name": "VINYL_DB_URL",
         "value": "postgresql+psycopg://vinyl:$(VINYL_DB_PASSWORD)"
                  "@postgres.infra.svc.cluster.local:5432/vinyl"},
        {"name": "VINYL_S3_ACCESS_KEY",
         "valueFrom": {"secretKeyRef": {"name": "vinyl-secrets", "key": "s3-access-key"}}},
        {"name": "VINYL_S3_SECRET_KEY",
         "valueFrom": {"secretKeyRef": {"name": "vinyl-secrets", "key": "s3-secret-key"}}},
        {"name": "VINYL_HA_TOKEN",
         "valueFrom": {"secretKeyRef": {"name": "vinyl-secrets", "key": "ha-token"}}},
        {"name": "VINYL_HA_URL", "value": os.environ.get("VINYL_HA_URL", "https://home.meklab.net")},
        {"name": "VINYL_HA_NOTIFY_SERVICE",
         "value": os.environ.get("VINYL_HA_NOTIFY_SERVICE", "")},
        # settings() requires these even though the QC path never uses them
        {"name": "VINYL_API_KEY", "value": "unused-in-qc-job"},
        {"name": "VINYL_DISCOGS_TOKEN", "value": "unused-in-qc-job"},
        {"name": "CAPTURE_ID", "value": str(capture_id)},
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app": "vinyl-qc", "capture": str(capture_id)[:8]}},
        "spec": {
            "ttlSecondsAfterFinished": 86400,
            "backoffLimit": 2,
            "template": {
                "metadata": {"labels": {"app": "vinyl-qc"}},
                "spec": {
                    "restartPolicy": "Never",
                    "imagePullSecrets": [{"name": "meklab-registry-pull"}],
                    "containers": [{
                        "name": "qc",
                        "image": image,
                        "command": ["/app/.venv/bin/python", "-m", "vinyl_server.qc"],
                        "env": secret_env,
                        "volumeMounts": [{"name": "scratch", "mountPath": "/scratch"}],
                        "resources": {
                            "requests": {"cpu": "500m", "memory": "512Mi"},
                            "limits": {"memory": "1Gi"},
                        },
                    }],
                    "volumes": [{"name": "scratch",
                                 "emptyDir": {"sizeLimit": "2Gi"}}],
                },
            },
        },
    }


def spawn_qc_job(capture_id: uuid.UUID) -> bool:
    image = os.environ.get("VINYL_IMAGE")
    if not image:
        log.warning("VINYL_IMAGE unset; not spawning QC job for %s", capture_id)
        return False
    try:
        with open(f"{_SA}/token") as f:
            token = f.read()
        with open(f"{_SA}/namespace") as f:
            namespace = f.read().strip()
        manifest = qc_job_manifest(capture_id, image, namespace)
        resp = httpx.post(
            f"https://kubernetes.default.svc/apis/batch/v1/namespaces/{namespace}/jobs",
            headers={"Authorization": f"Bearer {token}"},
            json=manifest,
            verify=f"{_SA}/ca.crt",
            timeout=10,
        )
        if resp.status_code not in (200, 201, 202):
            log.error("QC job spawn %s: %s %s", capture_id, resp.status_code, resp.text[:300])
            return False
        log.info("QC job spawned for %s", capture_id)
        return True
    except Exception:
        log.exception("QC job spawn failed for %s", capture_id)
        return False
