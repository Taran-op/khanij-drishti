# KhanijDrishti production deployment

The same application image runs as API, asynchronous worker and scheduler. PostgreSQL 16 with
PostGIS is the system of record; object/PVC storage holds raw lake files and model artifacts. The
virtual mine is an optional simulator profile and must not be enabled for a real telemetry rollout.

## Local production rehearsal (Docker Compose)

1. Copy `.env.example` to `.env` and replace every placeholder with independently generated
   secrets. Keep `KD_DEMO_LOGIN=0` outside demonstrations.
2. Build and validate: `docker compose config`, then `docker compose build`.
3. Start the database and migration first: `docker compose up -d postgis migrate`.
4. Import an existing SQLite installation once, if needed:
   `docker compose run --rm migrate python -m kd.migrate import-sqlite /source-data`.
5. Start the platform: `docker compose up -d` and open `http://localhost:8080`.
6. The observability consoles bind only to localhost: Prometheus `:9090`, Grafana `:3000`.
7. Enable the clearly-labelled virtual telemetry source only for demos with
   `docker compose --profile simulator up -d virtual-mine`.

The API's `/health` endpoint is liveness only. `/ready` verifies the database, warehouse, models
and freshness. A deployment must not receive traffic until `/ready` returns 200.

## Kubernetes

Build and push the backend and frontend images, replace the example registry/image tags and host in
`deploy/k8s/base.yaml`, then create secrets from an external secret manager. Do not apply
`secret.example.yaml` with placeholders.

```text
kubectl apply -f deploy/k8s/base.yaml
kubectl apply -f deploy/k8s/monitoring.yaml   # Prometheus Operator clusters only
kubectl -n khanij-drishti get pods
```

Run the migration Job before rolling API/worker/scheduler images. The ingress expects a `kd-tls`
secret; use cert-manager or a certificate supplied by the organisation. Use an RWX volume (EFS,
Azure Files, Filestore) for shared artifacts, and encrypted RWO storage for PostGIS. For production,
prefer the cloud's managed PostgreSQL service with PostGIS and remove the included StatefulSet.

## Cloud mappings

| Capability | AWS | Azure | Google Cloud |
|---|---|---|---|
| Kubernetes | EKS | AKS | GKE |
| PostgreSQL/PostGIS | RDS PostgreSQL | Azure Database for PostgreSQL | Cloud SQL PostgreSQL |
| Shared artifacts | EFS / S3 sync | Azure Files / Blob sync | Filestore / GCS sync |
| Secrets | Secrets Manager + External Secrets | Key Vault CSI | Secret Manager CSI |
| TLS/DNS | ACM + ALB ingress | App Gateway + Key Vault | ManagedCertificate + HTTPS LB |
| Monitoring | Managed Prometheus/Grafana | Azure Monitor | Managed Prometheus |

Use private database endpoints, workload identity instead of static cloud keys, encryption at rest,
WAF/rate limiting at ingress, network policies, image vulnerability scanning and immutable tags.

## Backup and disaster recovery

The scheduler creates a nightly backup and performs a weekly restore drill. PostgreSQL archives use
`pg_dump` custom format; the drill validates the archive with `pg_restore --list`. File checksums and
metadata are recorded in each archive manifest. Copy archives to versioned, immutable off-site
object storage with lifecycle retention suited to the organisation's RPO.

Monthly, restore the latest backup into an isolated database and exercise `/ready`, a GIS lookup,
authentication, report generation and one forecast. Record the drill outcome and recovery time.
Never test a restore over the production database.

## Secret and TLS policy

- Generate at least 32 random bytes for `KD_TOKEN_SECRET`; rotation signs out active sessions.
- Use different long random values for PostgreSQL, Grafana, SMTP and SMS credentials.
- Inject secrets at runtime; do not bake them into images, ConfigMaps or Git.
- Terminate TLS 1.2+ at the ingress/load balancer and redirect HTTP to HTTPS.
- Set `KD_ALLOWED_ORIGINS` to the exact HTTPS frontend origin.
- Protect `/metrics` with `KD_METRICS_TOKEN` when it is reachable outside the private cluster and
  configure Prometheus's bearer token accordingly.
