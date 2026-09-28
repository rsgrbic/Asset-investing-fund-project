# `iep` chart values

Defaults in `helm/iep/values.yaml` (base). `helm/iep/values-hosted.yaml` layers
the cluster on top; later `-f` wins. Size profiles in `helm/iep/scale/`.

## Services

| service | port | local host | cluster host | image | tag |
|---|---|---|---|---|---|
| auth | 5000 | auth.iep.local | auth.20.215.32.142.sslip.io | iep-auth | `latest` locally, git SHA on the cluster |
| employee | 5001 | employee.iep.local | employee.20.215.32.142.sslip.io | iep-employee | `latest` locally, git SHA on the cluster |
| director | 5002 | director.iep.local | director.20.215.32.142.sslip.io | iep-director | `latest` locally, git SHA on the cluster |

## Scale profiles

Replicas (auth / employee / director), autoscaling bounds, and data volumes.

| | replicas | autoscaling | mysql / mongo / redis |
|---|---|---|---|
| `small` | 1 / 1 / 1 | employee 1–4 | 4Gi / 4Gi / 128mb |
| `medium` | 2 / 2 / 1 | employee 2–5 | 16Gi / 16Gi / 256mb |
| `large` | 2 / 3 / 1 | auth 2–6, employee 3–12 | 64Gi / 64Gi / 1024mb |

## Top level

| Key | Default | Description |
|---|---|---|
| `registry` | `""` locally, `ghcr.io/rsgrbic` on the cluster | Prefix for every service image. |

## `services.<auth|employee|director>`

| Key | Default | Description |
|---|---|---|
| `image.repository` | `iep-<name>` | |
| `image.tag` | `latest` | Git SHA on the cluster. |
| `replicaCount` | 1–3 by service and profile | Ignored while `autoscaling.enabled`. |
| `port` | 5000 / 5001 / 5002 | |
| `host` | `*.iep.local` | `*.20.215.32.142.sslip.io` on the cluster. |
| `resources.requests` / `.limits` | profile-sized | A cpu request is required for autoscaling. |
| `autoscaling.enabled` / `.minReplicas` / `.maxReplicas` / `.targetCPUUtilizationPercentage` | employee only, by profile | Target is a percentage of the cpu request. |

## `ingress`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | |
| `className` | `nginx` | |
| `clusterIssuer` | `""` locally, `letsencrypt` on the cluster | |
| `acmeEmail` | `""` locally | Also creates the ClusterIssuer when set. |
| `acmeServer` | Let's Encrypt production | |
| `annotations` | body size 2m, 120s timeouts | nginx settings for the whole Ingress. |

## `ingress-nginx`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` locally, `false` on the cluster | Whether this chart installs the controller. |

## `env`

| Key | Default |
|---|---|
| `EMPLOYEE_ROLE` | `employee` |
| `DIRECTOR_ROLE` | `director` |
| `VOTING_DEADLINE_SECONDS` | `3600` |
| `MYSQL_DATABASE` | `iep_auth` |

## `secrets`

| Key | Behaviour |
|---|---|
| `create` | `true` renders `iep-secret` from the values below; `false` expects it to exist. |
| `jwtSecretKey` / `mysqlRootPassword` / `sqlDatabaseUrl` / `directorPassword` | Required. Render fails while any holds `CHANGE_ME`. |
| `directorEmail` / `directorForename` / `directorSurname` | Seed identity for the director account. |

## `monitoring`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Renders the ServiceMonitors, alerts, and dashboard loading. |
| `path` / `interval` / `scrapeTimeout` | `/metrics` / 30s / 10s | |
| `exporters.enabled` | `true` | Runs the Redis exporter. |
| `exporters.redis.image` | `oliver006/redis_exporter:v1.89.0-alpine` | |

## `infra`

| Key | Default | Description |
|---|---|---|
| `mysql.image` / `mongo.image` / `redis.image` | `mysql:8.0` / `mongo:6` / `redis:7-alpine` | Single-Pod stores with PVCs. |
| `<store>.storage` | 4Gi–64Gi by profile | Azure bills 4 GiB minimum. |
| `<store>.storageClass` | cluster default locally, `managed-csi` on the cluster | |
| `ganache.enabled` | `true` | In-memory chain; restarts wipe it. |
| `ganache.host` | `ganache.iep.local` locally, empty on the cluster | |
| `ganache.defaultBalanceEther` / `.accounts` / `.gasLimit` | 10000 / 10 / 10000000 | |
