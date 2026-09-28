# IEP — Investment Fund Portfolio Manager

Three Flask microservices that run an investment-fund order flow end to end:
employees register assets to buy or sell, a director approves or rejects them
through an on-chain vote, and a portfolio report tracks the result.

## How it works

```
register / login ──▶  auth      :5000   users + JWT (MySQL / SQLite)
orders / search  ──▶  employee  :5001   buy/sell orders, asset search (MongoDB, Redis)
decision / report ─▶  director  :5002   approvals, portfolio report (MongoDB, Redis, Ganache)
```

The flow is: register → log in → create a buy or sell order → the director
decides on it via a blockchain vote → the report reflects the new portfolio
state.

- **auth** issues JWTs with a `role` claim. Employees register themselves; the
  director account is seeded at startup.
- **employee** puts new orders into Redis for the director to pick up, and
  searches finalized assets in MongoDB by name, category, date, or nested
  fields (`info.*`).
- **director** lists pending orders, deploys a small voting contract per
  decision, and writes the outcome to MongoDB — a buy becomes a new asset, a
  sell stamps the asset as sold, a rejection changes nothing. The report groups
  assets by category into money spent vs. earned.

## Repo layout

```
auth/  employee/  director/   the three services, each with its own Dockerfile
helm/iep/                     Helm chart: services, infra (MySQL, MongoDB,
                              Redis, Ganache), ingress, autoscaling, dashboards
tools/dashboards/             Go program that generates the Grafana dashboards
argocd/                       ArgoCD app-of-apps + cluster add-ons
tests/unit/                   unit tests (pytest)
loadtest/                     k6 load scenario
```

## Run it locally

Prerequisites: Docker, a local cluster (Docker Desktop or kind), Helm 3.8+.

```bash
docker build -t iep-auth:latest ./auth
docker build -t iep-employee:latest ./employee
docker build -t iep-director:latest ./director
helm dependency update ./helm/iep
```

Point the hostnames at your cluster (hosts file):

```
127.0.0.1 auth.iep.local employee.iep.local director.iep.local ganache.iep.local
```

```bash
helm upgrade --install iep ./helm/iep -n iep --create-namespace -f secrets.local.yaml
```

Configuration has two axes, each passed with `-f` (later wins): environment
(`values.yaml` for local, `values-hosted.yaml` for the AKS cluster) and size
(`scale/small|medium|large.yaml`). See [`helm/README.md`](helm/README.md) for
the full reference. One note: `director` always runs as a single replica,
because its vote listener lives inside that one pod.


## How it ships

Pushes to `main` run CI: unit tests, Docker builds of only the services that
changed (published to GHCR), then a validation of every environment × size
combination. The rendered manifests go on branch `deploy` , and ArgoCD syncs to that.
 Secrets come from Azure Key Vault via External Secrets on the hosted
cluster. Logs are pushed to Loki.

## Monitoring

Each service exports its
own metrics, Prometheus scrapes them, and the Grafana dashboards load themselves
from a ConfigMap.

Two custom dashboards are **generated** — a small Go
program in `tools/dashboards/` builds them with Grafana foundation SDK. Change a panel, re-run the generator,
commit the output. There is a community Redis dashboard, kept for comparison.

The chart also defines its own alerts, aimed at the failures this system can
actually have: a vote that never reaches a decision, an order queue that stops
draining, orders lost to a Redis restart, Redis nearing its memory cap, and the
usual disk, OOM, and ArgoCD-drift set.
