# Deploying DocFlow to AWS

The application is deployment-ready: S3 storage, split images, an instance-role
credential path, trusted-proxy handling and authenticated Flower. This document
is the runbook for standing it up.

Nothing here has been executed — it creates billable AWS resources, so it is
written to be run deliberately rather than triggered by a script in the repo.

---

## Topology

```
        Internet
           │
      ┌────▼─────┐        ┌──────────────┐
      │   ALB    │───────▶│  EC2: api    │──┐
      └──────────┘        │  + frontend  │  │
                          └──────────────┘  │
                                            ├──▶ RDS Postgres (pgvector)
                          ┌──────────────┐  ├──▶ ElastiCache Redis
                          │ EC2: workers │──┘
                          │ worker,      │
                          │ worker-ocr,  │──────▶ S3 (documents)
                          │ beat, flower │
                          │ flower-auth  │
                          └──────────────┘
```

**Two instances, not one.** The API is stateless and scales horizontally; the
workers are CPU-bound and scale on queue depth. They also run different images
— the API image is 411MB against the worker's 976MB, because only the workers
need tesseract, poppler and the embedding model.

**One beat process, ever.** Two would double-fire the reaper. If the worker
instance is ever placed in an autoscaling group, beat must move to its own
single-instance service.

---

## 1. Managed services

**RDS PostgreSQL 16.** pgvector ships with RDS from 15.2 onward; enable it once
with `CREATE EXTENSION vector;` — migration `0003` does this itself, so it only
needs the DB user to have rights to create extensions.

**ElastiCache Redis 7.** This is both the broker and the rate-limit store. Note
it holds queued work: if it is wiped, in-flight messages are lost. The reaper
recovers jobs left in `PROCESSING` and — since Phase 6 — jobs left in `PENDING`
too, so a Redis loss degrades to delayed rather than dropped work.

**S3 bucket.** Block public access, default encryption on, versioning
optional. A lifecycle rule expiring objects after N days is worth adding: the
extracted text lives in Postgres, so the original upload is only needed while
a job might still retry.

---

## 2. IAM

The worker and API instances need an instance role — **not** access keys in the
environment. `core/storage.py` never reads a credential; boto3 resolves the
role automatically.

Minimum policy, scoped to the one bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
    "Resource": "arn:aws:s3:::YOUR-BUCKET/uploads/*"
  }]
}
```

`s3:ListBucket` is not required — the application only ever addresses objects
by key.

---

## 3. Configuration

Everything is environment-driven. On each instance:

```bash
DATABASE_URL=postgresql+psycopg2://USER:PASS@your-rds-endpoint:5432/docflow
REDIS_URL=redis://your-elasticache-endpoint:6379/0
S3_BUCKET=your-bucket
AWS_REGION=eu-west-2
FLOWER_USER=...
FLOWER_PASSWORD=...
FLOWER_SESSION_SECRET=...   # python -c "import secrets; print(secrets.token_urlsafe(32))"
NEXT_PUBLIC_API_BASE_URL=https://api.your-domain
TRUSTED_PROXY_COUNT=1
```

`TRUSTED_PROXY_COUNT=1` matters behind an ALB. Left at 0, every request appears
to come from the load balancer and the whole service shares one rate-limit
bucket. Set it to the number of proxies in front of the API — see
`api/limiter.py` for why the count, rather than the header alone, is what makes
this safe.

Do not put `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in this file. Their
presence overrides the instance role, which is a downgrade.

---

## 4. Run

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  up -d --build api frontend
```

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  up -d --build worker worker-ocr beat flower flower-auth
```

Services are named explicitly because the base compose file also defines
`postgres`, `redis` and `minio` — in production those are the managed services
above, not containers.

Migrations run automatically on API start, with a bounded retry: RDS can report
healthy while still finishing recovery, and a single attempt fails there.

---

## 5. Ports and exposure

| Service | Exposure |
|---|---|
| `frontend` (3000) | public, behind the ALB |
| `api` (8000) | public, behind the ALB |
| `flower-auth` (8080) | **private** — VPN or SSH tunnel only |
| `flower` (5555) | not published at all; reachable only from `flower-auth` |

Flower can revoke and terminate tasks. It sits behind a login page
(`flowerauth/`) rather than HTTP basic auth, but it is an admin surface and
should not be internet-reachable regardless.

If it is ever exposed through the load balancer, terminate TLS in front of it:
the login form posts credentials, and the session cookie is only as safe as the
transport carrying it.

---

## 6. Verifying a deployment

```bash
curl -fsS https://api.your-domain/health
```

Expect `{"status":"ok","database":"ok","broker":"ok"}` — a 503 names which
dependency is unreachable.

Then upload a document through the dashboard and watch it reach `DONE`. That
one action exercises the API, Postgres, Redis, both worker pools, S3 and the
embedding model together.

---

## 7. What is deliberately not here

**No Terraform or CDK.** Infrastructure-as-code for a single-environment
portfolio deployment would be more code than the application, and would go
stale the moment it is not applied.

**No autoscaling policy.** The right signal is queue depth, which `GET /stats`
already exposes; wiring it to a CloudWatch metric and a scaling policy is the
natural next step, and is a real piece of work rather than a config line.

**No secrets manager.** Environment files are adequate for one instance. Beyond
that, Secrets Manager or SSM Parameter Store, injected at container start.
