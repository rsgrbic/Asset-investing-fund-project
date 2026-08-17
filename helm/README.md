# `iep` Helm chart

Deploys the three Flask services (`auth`, `employee`, `director`), their backing
stores (MySQL, MongoDB, Redis), a Ganache dev-chain, and a single Ingress that
routes all three by hostname.

```
auth.<domain>      ->  auth      :5000
employee.<domain>  ->  employee  :5001
director.<domain>  ->  director  :5002
```

## Prerequisites

| | |
|---|---|
| Helm | 3.8+ |
| Kubernetes | 1.25+ |
| metrics-server | only if `autoscaling.enabled` is set anywhere |
| Subcharts | `ingress-nginx` 4.15.x, `cert-manager` v1.21.x — pulled by `helm dependency update` |

```bash
helm dependency update ./helm/iep
```

Run this once before the first install, and again whenever `Chart.yaml`
dependencies change. It writes `helm/iep/charts/` and `Chart.lock`.

## Layout

```
helm/
├── iep/
│   ├── Chart.yaml              chart metadata + the two subchart dependencies
│   ├── values.yaml             DEFAULTS = local (Docker Desktop)
│   ├── values-hosted.yaml      the AKS cluster
│   ├── scale/
│   │   ├── small.yaml          size profiles — see "Two axes" below
│   │   ├── medium.yaml
│   │   └── large.yaml
│   └── templates/
│       ├── configmap.yaml      non-secret env, shared by all three services
│       ├── secret.yaml         rendered only when secrets.create is true
│       ├── app/                deployment, service, ingress, hpa, clusterissuer
│       └── infra/              mysql, mongo, redis, ganache, storage (PVCs)
└── secrets.local.yaml.example  template for the gitignored real thing
```

## Two axes

Configuration is split across two independent dimensions, each a separate `-f`.

**Axis 1 — environment.** *Where* it runs. `values.yaml` (local) is always
loaded by Helm; `values-hosted.yaml` layers the cluster on top. The hosted file
is written out in full rather than as a sparse patch, so it can be read alone.

**Axis 2 — scale.** *How big the business is.* The `scale/` profiles set
replica counts, HPA ranges, resource requests and volume sizes. They are
optional; omitting one gives you the environment file's own values.

Order matters — later `-f` wins:

```bash
helm upgrade --install iep ./helm/iep -n iep --create-namespace \
  -f helm/iep/values-hosted.yaml \
  -f helm/iep/scale/medium.yaml
```

### Scale profiles

| | replicas a/e/d | HPA | cpu request a/e/d | mysql / mongo / redis |
|---|---|---|---|---|
| `small` | 1 / 1 / 1 | none | 50m / 50m / 50m | 4Gi / 4Gi / 1Gi |
| `medium` | 2 / HPA / 1 | employee 2–5 | 100m / 100m / 150m | 16Gi / 16Gi / 4Gi |
| `large` | HPA / HPA / 1 | auth 2–6, employee 3–12 | 200m / 200m / 300m | 64Gi / 64Gi / 16Gi |

`director` stays at 1 in every profile. See the constraint below.

## Running it

**Local** — build the images into the cluster's own daemon first, since
`registry` is empty and `imagePullPolicy` is `IfNotPresent`. Each service is its
own build context, so the image names must be given explicitly:

```bash
docker build -t iep-auth:latest ./auth && docker build -t iep-employee:latest ./employee && docker build -t iep-director:latest ./director
```

Point the three hostnames at the ingress controller by adding to your hosts file
(`C:\Windows\System32\drivers\etc\hosts`):

```
127.0.0.1 auth.iep.local employee.iep.local director.iep.local ganache.iep.local
```

Then install:

```bash
helm upgrade --install iep ./helm/iep -n iep --create-namespace -f secrets.local.yaml
```

**Uninstall.** This deletes the PVCs too — the databases go with it:

```bash
helm uninstall iep -n iep && kubectl delete namespace iep
```

## Publishing the chart

GHCR is an OCI registry, so no chart repo index or `gh-pages` branch is involved.
CI does this on every push to `main`; by hand it is:

```bash
helm registry login ghcr.io -u rsgrbic --password-stdin
```
```bash
helm dependency build ./helm/iep && helm package ./helm/iep -d dist && helm push dist/iep-0.5.0.tgz oci://ghcr.io/rsgrbic/charts
```

**`helm push` appends the chart name to the URL.** Pushing to
`oci://ghcr.io/rsgrbic/charts` lands the chart at
`oci://ghcr.io/rsgrbic/charts/iep:0.5.0`. Putting `iep` in the URL yourself
produces `.../charts/iep/iep`.

**The tag is `version:` from `Chart.yaml`, never a flag.** Pushing twice without
bumping it silently overwrites the published chart — so bump `version:` in the
same commit as any change to `templates/` or `values*.yaml`.

Consume it with:

```bash
helm install iep oci://ghcr.io/rsgrbic/charts/iep --version 0.5.0 -n iep --create-namespace
```

## Values

### Top level

| Key | Default | Description |
|---|---|---|
| `registry` | `""` | Prefix applied to every service image. No trailing slash; the template adds it. Empty means plain names, which is what locally built images need. |

### `services.<auth\|employee\|director>`

| Key | Description |
|---|---|
| `image.repository` | Image name, appended to `registry`. |
| `image.tag` | `latest` locally, where images are built straight into the cluster's daemon. On hosted this is the **git SHA**, rewritten and committed by CI on every push to main — do not edit it by hand. |
| `replicaCount` | Ignored when `autoscaling.enabled` — the template omits `replicas` entirely so the HPA owns the field. |
| `port` | Container port; the Service and Ingress backend both follow it. |
| `host` | Ingress hostname for this service. |
| `resources.requests` / `.limits` | A `cpu` request is mandatory for autoscaling — the HPA computes utilisation against it. Without one it silently does nothing. |
| `autoscaling.enabled` | Renders an HPA for this service. |
| `autoscaling.minReplicas` / `.maxReplicas` | Bounds. |
| `autoscaling.targetCPUUtilizationPercentage` | Percentage of the **request**, not the limit. |

### `ingress` — our Ingress resource

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | |
| `className` | `nginx` | Must match the controller's IngressClass. |
| `clusterIssuer` | `""` | Name of the cert-manager ClusterIssuer to request certificates from. Setting it turns TLS on. |
| `acmeEmail` | `""` | Setting this *in addition* makes the chart create that ClusterIssuer. Leave empty to reference one you made yourself. |
| `acmeServer` | Let's Encrypt prod | Swap for `https://acme-staging-v02.api.letsencrypt.org/directory` while debugging — far looser rate limits, untrusted certs. |
| `annotations` | body size 2m, 120s timeouts | nginx settings applied to the whole Ingress. |

### `ingress-nginx` — the controller (subchart)

Two different things that get confused constantly: this block is the **controller**,
the nginx Pods doing the routing. The `ingress` block above is the **rule** they
read. A rule with no controller running is inert — no error, no traffic.

Set `enabled: false` if the cluster already has a controller; it is cluster-scoped
and two would collide over the IngressClass.

### `cert-manager` (subchart)

Off locally, and not out of laziness: Let's Encrypt validates a hostname by
fetching a token from it over the public internet, and `*.iep.local` resolves
only on your machine. There is no way to get a real certificate for a local name.

### `env`

Non-secret environment variables, rendered into one ConfigMap and injected into
all three services. Changing a value here rolls the Deployments, via a
`checksum/config` pod annotation.

### `secrets`

| `create` | Behaviour |
|---|---|
| `true` (local) | Chart renders Secret `iep-secret` from the values below it. Supply real values with a gitignored `-f secrets.local.yaml` — see `secrets.local.yaml.example`. |
| `false` (hosted) | Chart renders nothing and expects `iep-secret` to already exist in the namespace, holding `JWT_SECRET_KEY`, `SQL_DATABASE_URL`, `MYSQL_ROOT_PASSWORD`, `DIRECTOR_EMAIL`, `DIRECTOR_FORENAME`, `DIRECTOR_SURNAME`, `DIRECTOR_PASSWORD`. |

On the cluster that Secret is produced by the External Secrets Operator, which
reads Azure Key Vault and authenticates through workload identity — a federated
token, so no credential is stored in git or in the cluster.

### `infra`

Single-Pod MySQL / MongoDB / Redis with PVCs, plus Ganache. No HA — adequate
here, not production.

| Key | Description |
|---|---|
| `<store>.image` | |
| `<store>.storage` | PVC size. Azure's minimum managed disk is 4 GiB; smaller requests are rounded up and billed at 4 GiB. |
| `<store>.storageClass` | `""` = cluster default (hostpath locally); `managed-csi` on AKS. |
| `<store>.resources` | |
| `ganache.enabled` | In-memory chain, no `--db`: every restart wipes contracts and balances. |
| `ganache.host` | Ingress hostname for the JSON-RPC endpoint. **Empty on any internet-reachable cluster** — Ganache has no authentication at all. `director` reaches it in-cluster at `http://ganache:8545` regardless. |
| `ganache.defaultBalanceEther` / `.accounts` / `.gasLimit` | |

## Constraints

**`director` must stay at `replicaCount: 1`, autoscaling off.** `POST /decision`
spawns a background thread that waits for the on-chain `Finalized` event before
writing to MongoDB. That thread lives in one Pod. With two replicas the vote can
be finalised by a Pod that has no listener, and the write never happens. Fixing
this means moving the listener into a worker Deployment that consumes from Redis
— an architectural change, not a values change.

**First install with cert-manager takes two passes.** cert-manager v1.21 ships
its CRDs as ordinary templates rather than in a `crds/` directory, and Helm does
not wait for template-rendered CRDs to become Established. So the ClusterIssuer
can be submitted before the API server knows what one is:

```
no matches for kind "ClusterIssuer" in version "cert-manager.io/v1"
```

Install once with `--set ingress.clusterIssuer=""`, then re-run normally. Two
commands, once per cluster.

**All three hostnames must resolve publicly before `clusterIssuer` is set.** One
certificate covers all three, so if one hostname is wrong, none of them get TLS.
