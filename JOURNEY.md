# IEP Moj — migration journal

A chronological record of turning a locally-deployed Flask microservice project into a
production-*shaped* cloud-native deployment on Azure. Written as a personal reference for the
thesis: it records not just what was built, but what was **considered and rejected**, what
**broke**, and how each failure was diagnosed.

Scope: 18 June 2026 (initial project) → 19 August 2026 (GitOps on AKS, deploying from rendered
manifests). §0–10 were written on 18 Aug; §11 continues from there.

Related documents: `plan.md` (forward roadmap), `helm/README.md` (chart reference),
`helm/README.local.md` (long-form Helm walkthrough). **This file is what actually happened.**

---

## 0. Starting point

Three Flask microservices plus a pytest integration grader:

| Service | Port | Backing stores |
|---|---|---|
| `auth` | 5000 | MySQL |
| `employee` | 5001 | MongoDB, Redis |
| `director` | 5002 | MongoDB, Redis, Ganache (blockchain) |

Deployment was raw manifests in `kubernetes/`, each service exposed by its **own LoadBalancer**, on
a local Docker Desktop cluster. `docker-compose.yml` for local testing.

The single biggest structural change of the whole project: **three LoadBalancers became one
Ingress**. Locally that is a style choice; on Azure each LoadBalancer is a separately billed public
IP, so it is also the first cost saving.

### Constraint discovered early — `director` cannot be replicated

`POST /decision` spawns an in-memory background thread that waits for the on-chain `Finalized`
event before writing to MongoDB. With two replicas the vote can be finalised by a pod holding no
listener and the write never happens; a restart loses in-flight approvals.

`director` is therefore pinned to `replicaCount: 1` in **every** scale profile. Fixing it properly
means moving the listener into a worker Deployment consuming from Redis — an architectural change,
not a config one. This is the system's scaling ceiling.

---

## 1. Helm chart

**Goal:** one chart rendering both the laptop deployment and the cluster deployment from different
values files, so moving to AKS is a values change rather than a rewrite.

### Decisions and rejected alternatives

| Decision | Rationale | Rejected |
|---|---|---|
| No `_helpers.tpl` | Three labels read better inline than behind an `include` | Standard Helm scaffolding |
| Keep HPA | Needed later for custom metrics from Prometheus | — |
| **Drop PodDisruptionBudget** | Pointless below three nodes — draining the only node evicts everything regardless. Also HPA scale-down bypasses the Eviction API, so a PDB does not constrain it | Keeping PDBs "for completeness" |
| One Ingress, **host-based** routing | The Flask apps serve from root (`/register`, not `/auth/register`), so path routing would need `rewrite-target` | Path-based routing |
| Ship `ingress-nginx` + `cert-manager` as chart dependencies | One `helm install` produces a reachable deployment | Separate manual installs |

### The requirement that was misread

*"Helm chart za različite redove veličina"* was first implemented as **environment tiers**
(dev/staging/prod). That was wrong. It means **deployment scale for different sizes of business** —
the same application sized for a small firm versus a large one.

Environment and scale are **orthogonal**, so they became two independent axes stacked with `-f`:

- **Environment** — *where it runs*: `values.yaml` (local), `values-hosted.yaml` (AKS)
- **Scale** — *how big the business is*: `scale/small.yaml`, `medium.yaml`, `large.yaml`

Final profiles, resized once after the first `large` was judged unrealistic:

| | replicas a/e/d | HPA | cpu request | mysql / mongo / redis |
|---|---|---|---|---|
| small | 1 / 1 / 1 | none | 50m | 4Gi / 4Gi / 1Gi |
| medium | 2 / HPA / 1 | employee 2–5 | 100m | 16Gi / 16Gi / 4Gi |
| large | HPA / HPA / 1 | auth 2–6, employee 3–12 | 200m | 64Gi / 64Gi / 16Gi |

### Problems encountered

**Infra pods lost their selector labels.** A mechanical `sed` refactor stripped the label helper
from the four infra templates, so Deployments no longer matched their own pods. Caught by reading
rendered output — `helm lint` does not check selector/label correspondence.

**`values-hosted.yaml` accumulated dead configuration.** Measured overlap:

| | keys |
|---|---|
| `values.yaml` | 94 |
| `values-hosted.yaml` | 60 |
| identical in both | 36 (pure duplication) |
| genuinely different | 23 |

Of those 23, **12 were dead** — `resources`, `replicaCount` and `infra.*.storage` are owned by the
scale profiles, which load *after* `values-hosted.yaml` and therefore win:

```
values-hosted.yaml says auth cpu=100m, replicas=2, redis 4Gi
rendered (hosted + small):  replicas: 1   storage: "1Gi"
```

Worse than duplication — a trap, since editing those values produces no effect. Cut the file from
148 lines to 59, having first verified rendering was byte-identical for all three hosted × scale
combinations before and after.

### Commands

```bash
helm lint ./helm/iep
helm template iep ./helm/iep -f helm/iep/values-hosted.yaml -f helm/iep/scale/small.yaml
helm install iep ./helm/iep --dry-run=server -n iep
```

---

## 2. Secrets — the longest decision

| Option | Verdict |
|---|---|
| **Sealed Secrets** | Encrypted secret committed to git, decrypted in-cluster. Rejected — still a ciphertext blob in git, manual key rotation |
| **Key Vault CSI driver** | Mounts secrets as files; would require app changes to read from disk rather than env |
| **SOPS + age** | Same "encrypted blob in git" shape, plus a key to manage |
| **External Secrets Operator + Azure Key Vault + workload identity** | **Chosen** — no credential stored anywhere; the pod authenticates with a federated OIDC token |

> **Correction recorded for honesty:** Sealed Secrets was initially written up as "rejected" more
> forcefully than the evidence supported. Both are defensible. ESO was chosen because the project's
> stated preference is architectures with *no stored credential at all* — not because Sealed
> Secrets is unworkable.

The chart supports both via one switch:

- `secrets.create: true` (local) — chart renders the Secret from a gitignored `-f secrets.local.yaml`
- `secrets.create: false` (hosted) — chart renders nothing; expects `iep-secret` to exist

**A guard was added** after noticing a silent failure: forgetting `-f secrets.local.yaml` installed
a perfectly working cluster whose JWT signing key was the literal string `CHANGE_ME_dev_only`.

```gotemplate
{{- if contains "CHANGE_ME" (get $.Values.secrets $key | toString) }}
{{- fail (printf "secrets.%s is still a placeholder..." $key) }}
{{- end }}
```

This immediately broke CI, which renders `values.yaml` with its placeholders — fixed by passing
throwaway `--set secrets.*=ci` values so the Secret template is still exercised rather than skipped.

---

## 3. Docker build restructuring

Originally all three images built from the **repo root** as context, with dockerfiles doing
`COPY auth/requirements.txt`. Every build uploaded the entire repository — `.venv/` and `.git/`
included — to the daemon in order to copy two files out of it. There was no `.dockerignore` at all.

Changed to per-service contexts with a `.dockerignore` in each service directory:

```bash
docker build -t iep-auth:latest ./auth
```

Verified the resulting images contain only what they should:

```
/app -> app.py  models.py  requirements.txt
```

**Side benefit:** the file is lowercase `dockerfile`, but `docker-compose.yml` referenced
`auth/Dockerfile` (capital D). That works on case-insensitive Windows and **fails on Linux CI
runners**. Per-service contexts removed the path reference entirely.

---

## 4. Azure foundation

### Region — first failure

```powershell
az aks create --resource-group iep-rg --name iep-aks --node-count 1 --generate-ssh-keys
```

```
(RequestDisallowedByAzure) Resource 'iep-aks' was disallowed by Azure: This policy maintains a
set of best available regions where your subscription can deploy resources.
```

Azure-for-Students subscriptions are restricted to a subset of regions. `westeurope` was not among
them; rebuilt in **`polandcentral`**. Allowed regions are discoverable with:

```powershell
az policy assignment list --query "[].parameters" -o json
```

### Registry — ACR rejected in favour of GHCR

| Option | Verdict |
|---|---|
| **ACR Basic** | ~$5/mo from student credit, native AKS auth via managed identity. Rejected — dies with the credit |
| **GHCR** | **Chosen.** Free for public repos, `GITHUB_TOKEN` minted per CI run so nothing is stored, holds the Helm chart as an OCI artifact natively |
| **Docker Hub** | Free-tier pull rate limits can stall a cluster mid-rollout |
| **JFrog Artifactory** | Named in the requirements; more setup for no gain here |

Consequence: `az aks create --attach-acr` was never needed.

### External Secrets Operator — the full trust chain

> AKS mints a token for one specific service account → Azure AD trusts it because of a federated
> credential → that identity has read access to Key Vault → ESO pulls the secrets → writes
> `iep-secret` → the chart mounts it, unchanged.

```powershell
az aks update -g iep-rg -n iep-aks --enable-oidc-issuer --enable-workload-identity
az aks show -g iep-rg -n iep-aks --query oidcIssuerProfile.issuerUrl -o tsv
az identity create -g iep-rg -n iep-eso-identity
az role assignment create --role "Key Vault Secrets User" --assignee-object-id PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope VAULT_RESOURCE_ID
az identity federated-credential create --name eso-federated -g iep-rg --identity-name iep-eso-identity --issuer ISSUER_URL --subject system:serviceaccount:external-secrets:external-secrets --audience api://AzureADTokenExchange
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace -f cluster-config/eso-values.yml
kubectl apply -f cluster-config/eso-secrets.yaml
```

**Four gotchas that cost time:**

1. **Key Vault secret names cannot contain underscores** — alphanumerics and hyphens only.
   `JWT_SECRET_KEY` is stored as `jwt-secret-key`; the `ExternalSecret` maps `remoteRef.key`
   (hyphens, Key Vault) → `secretKey` (underscores, what the app reads).
2. **Creating an RBAC-enabled vault does not let you write secrets to it.** A separate
   `Key Vault Secrets Officer` assignment on your own account is required first.
3. **`--subject` is exact and unforgiving** — `system:serviceaccount:<namespace>:<serviceaccount>`.
   One wrong character gives a generic auth failure with no hint.
4. **ESO needs both** a service-account annotation (`azure.workload.identity/client-id`, naming
   *which* identity) and a pod label (`azure.workload.identity/use: "true"`, which triggers the
   webhook injecting the token). With only the annotation everything looks configured and
   authentication silently fails.

### Problem: mongo never became Ready

```
Readiness probe failed: command timed out: "mongosh --quiet --eval db.adminCommand('ping').ok"
timed out after 1s        (x11483 over 15h)
```

`timeoutSeconds` defaults to **1 second**. `mongosh` is a Node.js binary needing 1–2s just to start,
so the probe was killed before mongo was even asked anything. It passed locally only because a dev
laptop starts Node faster than a 2-vCPU node running ten other pods.

Fix: `timeoutSeconds: 10`. **Lesson:** probe timeouts must suit the binary being executed, and a
probe that passes on a laptop proves nothing about a small cloud node.

### Problem: the public IP black-holed all traffic

The hardest failure of the project, and the most interesting for the thesis.

Symptom: `curl` to the LoadBalancer's public IP never established a TCP connection — from two
different networks. No HTTP error, no logs, nothing visibly wrong in the cluster.

Everything checked out:

```
controller pod   Running, 1/1 Ready
endpoints        10.244.0.133:80,:443   (populated)
Ingress          ADDRESS assigned
NSG              Allow Internet -> 20.215.101.37 on 80,443
LB rule          frontend 80 -> backend 80, enableFloatingIP: true
backend pool     1 member (the node NIC)
```

The cause was the last thing left to check:

```powershell
az network lb probe list -g MC_iep-rg_iep-aks_polandcentral --lb-name kubernetes -o table
# Proto  Port    Path
# Http   32593   /
```

Azure health-probes the nodePort with `GET /` and accepts only **2xx/3xx**. ingress-nginx answers
**404** there, because a health probe carries no Host header matching any Ingress rule. Every node
was marked unhealthy, so Azure's load balancer dropped all traffic to the frontend IP — silently,
and failing *closed*.

```yaml
service.beta.kubernetes.io/azure-load-balancer-health-probe-request-path: /healthz
```

`/healthz` is ingress-nginx's own `controller.healthCheckPath`, served by its default server block
regardless of Host — **not** a stock nginx endpoint.

**Why this matters for the write-up:** the chart was valid Kubernetes, every pod Ready, endpoints
populated. The failure lived entirely in how a *cloud provider* decides a node is healthy. Local
testing cannot surface it, because Docker Desktop has no cloud load balancer.

### Problem: `helm upgrade` conflicts with AKS

```
conflict occurred while applying object /iep-cert-manager-webhook: conflict with
"admissionsenforcer" ... .webhooks[name="webhook.cert-manager.io"].namespaceSelector
```

AKS runs an `admissionsenforcer` component that appends `namespaceSelector` exclusions to every
third-party ValidatingWebhookConfiguration, so a webhook with `failurePolicy: Fail` cannot lock
Azure out of its own managed namespaces. It takes field ownership; Helm 4 uses server-side apply and
refuses to overwrite.

Worked around with `--force-conflicts`. It recurs on every upgrade — the real fix arrived in stage 4.

### TLS

Hostnames use `sslip.io` wildcard DNS (`<service>.<ip>.sslip.io`), so no domain purchase. Staging
Let's Encrypt was used first, deliberately: production allows only 5 failed validations per hostname
per hour, staging is ~10× looser with identical failure modes.

**Problem: switching staging → production is not just changing `acmeServer`.** After deleting the
`iep-tls` Secret and re-running, the certificate was *still* staging-issued:

```powershell
kubectl get order -n iep -o jsonpath="{range .items[*]}{.status.url}{'\n'}{end}"
# https://acme-staging-v02.api.letsencrypt.org/acme/order/...
```

cert-manager had reused the existing `Certificate` revision and its in-flight Order, which still
pointed at staging. **The Order's `.status.url` is the definitive answer to "which endpoint issued
this."**

Fix — delete the account key and the Certificate, not just the Secret:

```powershell
kubectl delete secret letsencrypt-account-key -n iep
kubectl delete certificate iep-tls -n iep
kubectl delete secret iep-tls -n iep
```

Verified on the wire:

```
issuer=C=US, O=Let's Encrypt, CN=YR2          (production; staging reads "(STAGING) ...")
subject=CN=auth.20.215.32.142.sslip.io
```

### Problem: images would not pull

Pods sat in `ImagePullBackOff`. The images existed and `docker manifest inspect` succeeded — but
only because the local machine was logged in to GHCR. Tested anonymously instead:

```bash
curl -s "https://ghcr.io/token?scope=repository:rsgrbic/iep-auth:pull&service=ghcr.io"
# then GET the manifest with that token -> HTTP 403
```

All four packages were **private**. The kubelet on the AKS node has no credentials, so it could not
pull. Fixed by making the packages public in GitHub's package settings.

---

## 5. CI with GitHub Actions

One workflow, `.github/workflows/ci.yml`, three jobs:

```
unit-tests ──→ images ──→ chart
```

| Job | Does | On PRs |
|---|---|---|
| `unit-tests` | `pytest tests/unit` — 48 cases over pure functions in `auth` and `director` | runs |
| `images` | matrix over three services, `docker/build-push-action`, GHA layer cache, tagged `${{ github.sha }}` + `latest` | **builds but does not push** |
| `chart` | `helm lint`, kind cluster, validates all 6 env × scale combos against a real API server, pins image tags, publishes chart | validates only |

### Design decisions

- **The push gate is on the step, not the job.** A job-level `if: main` would skip `images` on PRs,
  and skipping a job skips everything that `needs:` it — so PRs would lose chart validation
  entirely. Instead `push:` is an expression and the job always runs.
- **`Chart.yaml` is the release signal.** The chart publishes only when that file changed in the
  push, so an ordinary commit cannot silently overwrite a published version. *Auto-bumping from
  commit-message markers (`#minor`/`#major`) was designed and then rejected as too much ceremony.*
- **Image tags are written back to git.** The `chart` job rewrites `services.*.image.tag` in
  `values-hosted.yaml` to the commit SHA and commits it with `GITHUB_TOKEN`. That commit is what
  ArgoCD acts on — CI writes to git, ArgoCD reads from git, and the pipeline never holds cluster
  credentials.
- **`sed`, not `yq`, for the tag rewrite.** `yq` preserves comments but **strips blank lines**,
  producing formatting noise on every run. A targeted `sed` on the three `      tag:` lines gives a
  clean three-line diff, with a guard that fails the build if it does not rewrite exactly 3.

### What is unit-tested, and what is not

Only genuinely pure functions — no mocks, no fixtures, no fake databases:

| Module | Functions | Cases |
|---|---|---|
| `auth/models.py` | `hash_password`, `verify_password` | 8 |
| `auth/app.py` | `_is_valid_email`, `_is_valid_password`, `_get_str` | 24 |
| `director/app.py` | `_is_valid_uuid`, `_is_eth_address`, `_now_iso` | 16 |

**`employee` has no unit tests, deliberately.** Its helpers (`_build_mongo_query`, `_is_iso8601`,
`_to_iso_z`) are nested inside `create_app()`, so reaching them requires constructing the app, which
requires a Mongo connection. Testing them would be mock-wrangling. The fix is moving them to module
level, not writing mocks.

**The pytest grader does not run in CI.** It needs all three services plus MySQL, Mongo, Redis and
Ganache; standing that up in a runner was judged not worth the time versus running it by hand
against the real cluster.

One test assumption was wrong and the code was right: `Web3.is_address` accepts an address with no
`0x` prefix. The test was corrected to pin that behaviour rather than "fixing" the code.

### The most important CI finding

**Nothing in the Helm toolchain validates field *values*.** A deliberately corrupted Service type
(`type: loadBalan`) was accepted by:

| Check | Catches it? |
|---|---|
| `helm lint` | No |
| `helm template` | No |
| `helm install --dry-run=server` | **No** |
| `helm template \| kubectl apply --dry-run=server` | **Yes** |

```
Service "ganache" is invalid: spec.type: Unsupported value: "loadBalan":
supported values: "ClusterIP", "ExternalName", "LoadBalancer", "NodePort"
```

Helm's `--dry-run=server` only performs **API discovery** — it catches an unknown *kind* (which is
how the missing `ClusterIssuer` CRD was caught earlier) but never submits objects for field
validation. `kubectl apply --dry-run=server` does, because it is a real admission request that
stops short of persisting.

Hence the CI job spins up a kind cluster purely to have a real API server to validate against.

**And `helm lint` alone is insufficient** because it renders with default values only. Under
`values.yaml` the HPA, ClusterIssuer and ganache-ingress branches never execute:

```
HPAs rendered by lint's defaults:  0
HPAs with scale/large.yaml:        2
```

The six combinations exist to force every conditional path.

### CI problems and fixes

| Problem | Cause | Fix |
|---|---|---|
| `no repository definition for https://...` | `helm dependency build` resolves `Chart.lock`'s URLs through a **registered** repo; a fresh runner's `repositories.yaml` is empty | `helm repo add` before `dependency build`. (`dependency update` does not need this, but re-resolves version ranges and rewrites the lock — rejected for reproducibility) |
| `ImportError: cannot import name 'ContractName' from 'eth_typing'` | web3 6.5.0 registers a pytest plugin loaded at startup that imports a symbol removed in eth-typing 5 | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` — version-independent. The `eth-typing<5` pin alone proved insufficient |
| `Cache export is not supported for the docker driver` | The runner's default buildx driver cannot export a cache | `docker/setup-buildx-action@v3` creates a `docker-container` driver |
| `GITHUB_TOKEN` cannot write to the registry | Packages created by hand with a PAT are not linked to the repository | Grant Actions access per package; add `org.opencontainers.image.source` so future packages auto-link |
| `namespace from the provided object "default" does not match "iep"` | `helm template` without `-n` renders `namespace: default` into subchart objects | `helm template -n iep`, **and no `-n` on `kubectl`** — a few subchart objects target `kube-system` and any `-n` conflicts with those |

---

## 6. ArgoCD (GitOps)

**Concept:** GitOps inverts the deploy direction. Nothing pushes to the cluster; the cluster *pulls*
its desired state from git and reconciles continuously.

**Key mechanical fact:** ArgoCD runs `helm template` and applies the output — it never creates a
Helm release. So `helm list` will not show the app, `helm rollback` stops being the rollback
mechanism (`git revert` is), and Helm's `lookup` does not work.

### Structure — app-of-apps

```
argocd/root.yaml  ──watches──>  argocd/apps/
                                  ├── iep.yaml               (helm/iep from the git path)
                                  ├── cert-manager.yaml      (upstream chart, v1.21.1)
                                  └── ingress-nginx.yaml     (upstream chart, 4.15.1)
```

- **`iep` sources the git path, not the published OCI chart.** CI commits image tags into
  `values-hosted.yaml`, so a git source makes that commit the deploy trigger. An OCI source would
  need a `Chart.yaml` bump for every deploy.
- **Add-ons became their own Applications**, with `values-hosted.yaml` setting
  `ingress-nginx.enabled: false` and `cert-manager.enabled: false`. This stops `helm uninstall` of
  the app taking the ingress down, and makes a controller swap a one-line change.
- **Sync waves** (`argocd.argoproj.io/sync-wave: "-1"` on the add-ons) order CRDs before the
  ClusterIssuer, removing the two-pass cert-manager install entirely.
- **Manual sync to start**, so the first diff is read rather than applied blind.

### Preparation: pinning the public IP

Before handing the controller to ArgoCD, the public IP was made independent of it.

The original IP was `Static`, which misleads. Its tags told the real story:

```
k8s-azure-service:        iep/iep-ingress-nginx-controller
aks-managed-cluster-name: iep-aks
```

`Static` means the address does not rotate *while the resource exists* — it survives
`az aks stop/start`. But the resource is **AKS-managed and bound to that Service**, so replacing the
Service garbage-collects it. There is no supported way to convert it to user-managed.

A user-created IP in `iep-rg` (not the AKS-managed `MC_` group, which is deleted with the cluster)
has no such tag and no AKS lifecycle:

```powershell
az network public-ip create --resource-group iep-rg --name LBalancer_IP --sku Standard --allocation-method Static --location polandcentral
az role assignment create --assignee AKS_IDENTITY_PRINCIPAL_ID --role "Network Contributor" --scope RESOURCE_GROUP_ID
```

```yaml
service.beta.kubernetes.io/azure-pip-name: LBalancer_IP
service.beta.kubernetes.io/azure-load-balancer-resource-group: iep-rg
```

The address changes exactly once when you do this; never again, including across cluster rebuilds.
Because the hostnames are `sslip.io` names embedding the IP, this also meant one hostname change and
one certificate reissue — done deliberately, in advance, rather than as a surprise.

### Problem: adoption by name matching does not work

The plan was to name the Applications so their Helm release names produced the *same object names*
as the existing `iep` release, letting ArgoCD adopt in place with no downtime. Verified by
rendering:

| Application name | Renders | Live object |
|---|---|---|
| `iep-ingress-nginx` | `iep-ingress-nginx-controller` | same |
| `iep-cert-manager` | `iep-cert-manager`, `-cainjector`, `-webhook` | same |

The names matched. The sync failed anyway:

```
Deployment.apps "iep-cert-manager" is invalid: spec.selector: Invalid value:
{"matchLabels":{... "app.kubernetes.io/instance":"iep-cert-manager" ...}}: field is immutable
```

Both charts put `app.kubernetes.io/instance` — which Helm sets from the **release name** — inside
`spec.selector.matchLabels`. The live Deployments carry `instance: iep`; the new Applications render
`instance: iep-cert-manager`. **`spec.selector` is immutable on a Deployment.** Matching names was
necessary but not sufficient.

Fix — delete only the four Deployments and let ArgoCD recreate them:

```powershell
kubectl delete deploy iep-cert-manager iep-cert-manager-cainjector iep-cert-manager-webhook iep-ingress-nginx-controller -n iep
argocd app sync iep-cert-manager
argocd app sync iep-ingress-nginx
```

Deployments only — **not** the Services (which hold the public IP and have mutable selectors), and
**not** the CRDs. Roughly 30 seconds of ingress downtime.

**`Replace=true` was considered and rejected.** ArgoCD's sync option handles immutable fields by
delete-and-recreate, but it applies to *every* resource in the Application — including cert-manager's
CRDs, and replacing a CRD cascades to every `Certificate` and `ClusterIssuer` in the cluster. A
targeted `kubectl delete` of four Deployments is far safer than a blanket option that could take TLS
with it.

### Problem: ArgoCD and AKS fighting over the webhook

The `admissionsenforcer` conflict returned in a new form. The live webhook had **three**
`namespaceSelector` expressions; git had one:

```
chart says:  cert-manager.io/disable-validation NotIn [true]
AKS added:   control-plane NotIn [true]
AKS added:   kubernetes.azure.com/managedby NotIn [aks]
```

Field managers confirmed both writers: `admissionsenforcer -> Update` and `argocd-controller -> Update`.

This is a **permanent flap**, not a one-off: ArgoCD reverts to git, AKS re-patches, forever — and
with `selfHeal` enabled it never settles. AKS's additions are also *protective*: with
`failurePolicy: Fail`, a webhook that intercepts AKS-managed namespaces can lock Azure out of
managing its own components if the webhook pod is ever unhealthy.

The field genuinely has two legitimate owners, so ArgoCD concedes it:

```yaml
ignoreDifferences:
  - group: admissionregistration.k8s.io
    kind: ValidatingWebhookConfiguration
    jqPathExpressions:
      - .webhooks[]?.namespaceSelector
```

Note this is the *opposite* resolution from Helm's `--force-conflicts`, which **took** the field.
Conceding is correct here because AKS's version is the safer one.

### Other ArgoCD notes

- **Renaming an Application is a delete + create.** With the root on auto-sync, the old ones were
  pruned. Deletion was non-cascading (no `resources-finalizer` annotation), so no workloads were
  touched — worth knowing, because *with* that finalizer it would have deleted everything.
- **`SharedResourceWarning`** appears while two Applications both render the same object. It is a
  warning, not a block, and clears once ownership settles.
- **"requires pruning" is not a failure.** `argocd app sync iep` succeeds; the app simply stays
  `OutOfSync` while live resources exist that its manifest no longer describes.
- **Ownership is tracked by the `argocd.argoproj.io/tracking-id` annotation.** Checking it is the
  fastest way to see whether an adoption actually happened.

---

## 7. Final state

> **Superseded in part by §11.** The `CI` and `Deploy path` rows below describe the tag write-back
> that was replaced on 19 Aug 2026. Everything else in this table still holds.

| Piece | State |
|---|---|
| Cluster | AKS `iep-aks`, rg `iep-rg`, `polandcentral`, 1 × `Standard_D4ds_v4`, System pool |
| Public IP | `LBalancer_IP` = 20.215.32.142, user-owned in `iep-rg` |
| Hostnames | `<service>.20.215.32.142.sslip.io` |
| TLS | Production Let's Encrypt, trusted, expires 16 Nov 2026 |
| Images | `ghcr.io/rsgrbic/iep-<service>`, SHA-tagged, public |
| Chart | `oci://ghcr.io/rsgrbic/charts/iep` |
| Secrets | ESO → Azure Key Vault → workload identity. No stored credential |
| CI | 3 jobs, validates against a real API server, writes image tags back to git |
| CD | ArgoCD app-of-apps, 4 Applications, all Synced/Healthy |
| Deploy path | `git push` → CI → tag commit → ArgoCD sync. No `kubectl` |

Repository layout added during this work:

```
.github/workflows/ci.yml       three-job pipeline
argocd/root.yaml               app-of-apps root
argocd/apps/*.yaml             one Application per component
cluster-config/eso-*.yml       ESO values + ClusterSecretStore/ExternalSecret
helm/iep/                      the chart (12 templates, no _helpers.tpl)
helm/iep/scale/                small / medium / large
tests/unit/                    48 unit tests over pure functions
```

---

## 8. Reproducing this from zero

**Azure (outside Kubernetes — never ArgoCD's job):** resource group, AKS cluster, OIDC issuer,
Key Vault + secrets, managed identity, federated credential, role assignments, static public IP.

**Manual in-cluster bootstrap:**

```powershell
helm install argocd argo/argo-cd -n argocd --create-namespace
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace -f cluster-config/eso-values.yml
kubectl apply -f cluster-config/eso-secrets.yaml
kubectl apply -f argocd/root.yaml
```

**Everything else is ArgoCD's.**

This is the honest floor, and it is called the **bootstrap paradox**: ArgoCD cannot deploy the thing
that deploys ArgoCD, and it cannot apply the Application that tells it what to apply. Every GitOps
setup has those two steps; setups claiming zero usually have Terraform performing them.

Moving ESO into an Application would reduce the manual set to two commands. Not done — `eso-values.yml`
carries a per-identity client ID, which is environment binding rather than pure config.

**Local development is unaffected by any of this:**

```bash
docker build -t iep-auth:latest ./auth
helm upgrade --install iep ./helm/iep -n iep --create-namespace -f secrets.local.yaml
```

---

## 9. Open items and "what ifs"

### ingress-nginx is end-of-life

`kubernetes/ingress-nginx` **reached EOL in March 2026 and the repository is archived** — no further
security patches. `InGate`, the successor its maintainers proposed, was retired in the same
announcement; Gateway API is the sanctioned direction.

A migration to **Traefik v3** was researched and planned, then **deliberately deferred**. Traefik was
chosen over the alternatives because it consumes standard Ingress resources natively, so the routing
template, hostnames and cert-manager integration would survive unchanged.

**Ambassador/Emissary was rejected**: the open-source project is CNCF-incubating but Ambassador Labs
has stepped back from direct involvement. Migrating off a project that died from maintainer shortage
onto another whose corporate sponsor withdrew is not obviously progress.

Because the add-ons are now isolated ArgoCD Applications and the IP is pinned, the swap is a change
to `chart:` and `targetRevision:` plus three annotations — hostnames and certificate would survive.
The Azure health probe would need Traefik's **per-port** form:

```yaml
ingressRoute:
  healthcheck:
    enabled: true
    entryPoints: [web]
service:
  annotations:
    service.beta.kubernetes.io/port_80_health-probe_request-path: "/ping"
```

**Recorded as a known operational risk**, not a task.

### Other open questions

- **Hostnames.** An Azure DNS label (`asset-fund.polandcentral.cloudapp.azure.com`) was reserved but
  is unused: a public IP carries exactly **one** label, and the Ingress routes three services by
  host. Options were path-based routing on the single real name (needs `rewrite-target`, and
  `cloudapp.azure.com` is on the Public Suffix List so it gets its own Let's Encrypt quota) versus
  host-based `sslip.io`. **Host-based sslip.io was chosen** — no template change. The cost is
  `sslip.io`'s globally shared Let's Encrypt rate limit, a renewal risk currently carried.
- **CI write-back requires `git pull` before local work.** Options considered: `git config
  pull.rebase true` (chosen implicitly), a dedicated `deploy` branch so `main` never receives bot
  commits, or ArgoCD Image Updater with `write-back-method: argocd` (rejected — removes the image
  tag from git, defeating the point). **Resolved 19 Aug 2026 — see §11.2.** The write-back was
  removed entirely rather than made to work.
- **Digest vs tag pinning.** Images are deployed by git-SHA *tag*, not `@sha256:` *digest*. Tag
  immutability is a convention; digest immutability is a property. Kubernetes records the digest in
  `imageID` regardless, so digest-level traceability of the running pod exists for free. Digest
  pinning would need `repo@sha256:` (different separator) and only helps against a registry-compromise
  threat model.
- **Managed databases.** MySQL/Mongo/Redis are single-pod with PVCs — no HA, and a node drain takes
  them down. Azure Database / Cosmos DB / Azure Cache are the "correct" answer but would consume the
  student credit quickly.
- **Published chart is `0.5.0` while `Chart.yaml` says `1.0.0`** — the version bump has not yet
  produced a publish.
- **Stale Helm release records.** Seven `iep` release Secrets remain in the `iep` namespace from
  before ArgoCD took over. Inert, but `helm list` showing them is misleading:
  `kubectl delete secret -n iep -l owner=helm`.

### Cost management

```powershell
az aks stop --resource-group iep-rg --name iep-aks
az aks start --resource-group iep-rg --name iep-aks
```

**Scaling the node pool to 0 is not possible** — `nodepool1` is Mode `System`, and AKS requires at
least one node there. Only *User* pools can scale to zero. Stopping the cluster is the only way to
stop paying for compute.

Still billed while stopped: the static public IP, the load balancer, and three managed disks — a few
dollars a month against ~$30+ running.

Everything survives a stop: Kubernetes objects live in etcd, PVCs are managed disks, and the public
IP is user-owned. ArgoCD resumes reconciling on start and catches up on any commits made meanwhile.

---

## 10. Transferable lessons

1. **Local success proves less than it feels like.** The health-probe failure, the mongo probe
   timeout and the dockerfile casing issue were all invisible locally and all broke in the cloud.
2. **Validation layers are not interchangeable.** `helm lint` ⊂ `helm template` ⊂
   `helm install --dry-run=server` ⊂ `kubectl apply --dry-run=server` ⊂ a real install — and each
   catches a strictly different class of error.
3. **Cloud integrations fail closed and silently.** Azure's load balancer dropped 100% of traffic
   with no error anywhere in Kubernetes.
4. **Shared ownership of a field is a real state.** Both `--force-conflicts` (take it) and
   `ignoreDifferences` (concede it) are valid; which is correct depends on whose value is safer.
5. **Immutable fields constrain migrations.** `spec.selector` cannot be patched, so any change to
   Helm release naming forces delete-and-recreate.
6. **GitOps needs something to write to git.** ArgoCD reconciles; it does not decide what to deploy.
   Without the CI tag write-back, ArgoCD would faithfully deploy `:latest` forever.

---

## 11. After the first write-up — review, rendered manifests, Terraform

*Everything above was written on 18 Aug 2026. This section covers 19 Aug: a full-codebase review,
the removal of the CI write-back, and an evaluation of Terraform that was researched but not built.*

### 11.1 A review of the whole state, not just a diff

With the architecture stable, the whole tracked codebase was reviewed rather than a single change.
Ten findings survived verification, seven confirmed. The security ones chain:

**Authorization is bypassed by omitting a claim.** Both guards read

```python
role = claims.get("role", None)
if role is not None and not director_role == role:
    return jsonify({"msg": "Missing Authorization Header"}), 401
```

When a token carries no `role` claim at all, `role` is `None`, the condition short-circuits, and the
request is **authorized**. The check only rejects a *wrong* role, never a *missing* one. Present in
`director/app.py` and `employee/app.py` identically — the same function copy-pasted with one string
changed, which is also why the bug exists in both.

The correct form is two explicit checks: reject `None` first, then reject a mismatch. Collapsing it
to `if role != expected` works but hides the intent, which matters in code whose whole job is
saying no.

**And the signing key has a known fallback.** All three services do
`os.environ.get("JWT_SECRET_KEY", "HARDCODED")`. The chart's placeholder guard cannot see this — it
validates values files, not Python defaults. Any deployment path that forgets the env var signs
tokens with a literal that is now in a public repo. Combined with the above: a roleless token signed
with `"HARDCODED"` reaches every director endpoint.

**A ReDoS in search.** `_build_mongo_query` escapes user input for `contains`, `startswith` and
`endswith`, then passes it raw for `regex`. A pattern like `(a+)+$` against a long match hangs
MongoDB's regex engine server-side.

**A seed guard that checks the wrong thing.** `_seed_director` queries for a hardcoded email but
inserts the one from `DIRECTOR_EMAIL`, so the guard never matches its own row and every cold start
attempts a duplicate insert, caught only by `IntegrityError`.

Three infrastructure findings landed too — two of which are still open and recorded in §11.7.

**None of the application findings were fixed.** This is a deliberate boundary: the auth model is
the original coursework's, and the thesis is about the deployment architecture wrapped around it.
Recording them precisely is the deliverable; rewriting the app is not. That said, the role-guard
bug is the single best example in this project of a defect that **no amount of deployment rigour
catches** — it passes lint, tests, template validation, dry-run and sync.

### 11.2 Removing the CI write-back entirely

§9 recorded the write-back as an annoyance. Reviewing it found a real failure, not just friction:
two pushes landing close together both `sed` the same three `tag:` lines, and the second run's
rebase applies its change on top of the first's — a guaranteed conflict on identical lines. The
step fails, that commit's images are never pinned, and ArgoCD never deploys them. A green pipeline
would not have shown it; only the second run fails.

Four options were weighed:

| Option | Verdict |
|---|---|
| `git pull` before the `sed` | Works, minimal. Applied first as a stopgap |
| Separate config repo | Standard at scale; overkill for one environment |
| ArgoCD Image Updater | Smallest change that removes CI's write access, but the `argocd` write-back method takes the tag out of git |
| **Rendered manifests** | **Chosen** |

CI now runs `helm template` and commits the **output** to a `deploy` branch, which ArgoCD reads.
`main` never receives a bot commit. Because each run checks out the tip of `deploy` rather than its
own trigger SHA, there is nothing to rebase — the conflict is removed structurally rather than
handled.

What this bought beyond the fix: what ArgoCD applies is readable YAML in a diff, no subchart is
resolved from `charts.jetstack.io` at sync time, and `values-hosted.yaml` can no longer claim a tag
that is not deployed — its three entries are now `SET-BY-CI` and the SHA exists only in rendered
output.

What it cost: hand-installing the hosted config is no longer possible, ArgoCD's Helm parameter
surface is gone (it is a directory Application now), and there is no manual escape hatch — the
render is the only path to the cluster.

**Verified before pushing:** `secrets.create: false` means the whole body of `templates/secret.yaml`
sits inside an `{{- if }}`, so the render emits no Secret and the branch is safe to publish. Live
and rendered selectors matched exactly (`{app: auth}`), and the tracking ID
`iep:apps/Deployment:iep/auth` is keyed on the Application name — so this adopted in place, with no
repeat of the delete-and-recreate that §6 needed.

### 11.3 Scale profiles became directories

The first cut hardcoded `scale/small.yaml` in the render command. That put a **deployment** decision
inside the **build** pipeline, invisible from the Application — worse than the `valueFiles:` entry it
replaced.

Fix: CI renders all three profiles into `manifests/{small,medium,large}/`, and the Application's
`path:` selects one. Switching scale is a one-line commit to `argocd/apps/iep.yaml`, and the chosen
profile is legible in the path itself.

| Profile | Objects | HPA targets | mysql / mongo / redis |
|---|---|---|---|
| small | 20 | none | 4 / 4 / 1 Gi |
| medium | 21 | employee | 16 / 16 / 4 Gi |
| large | 22 | auth, employee | 64 / 64 / 16 Gi |

`director` stays at one replica with no HPA in all three — the constraint from `values.yaml` holds
across the whole matrix, which the render made checkable rather than assumed.

**The switch is a one-way ratchet.** PVCs cannot shrink. `managed-csi` allows expansion, so
small → medium → large works; going back down leaves ArgoCD permanently OutOfSync on the PVC unless
the volumes are deleted. This is a pre-existing property of the profiles that rendering merely made
visible.

### 11.4 A bug caught by reasoning rather than by running

The first version of the publish step used:

```bash
if git diff --quiet -- manifests/iep.yaml; then
```

`git diff` **ignores untracked files**. On the first run after bootstrapping the orphan branch,
`manifests/` does not exist in `HEAD`, so the comparison reports "no change", the step logs
`rendered output unchanged`, and pushes nothing. A green CI run and an empty deploy branch.

Fixed by staging first and comparing the index:

```bash
git add manifests
if git diff --cached --quiet; then
```

Worth recording because it is the same class as the `.helmignore` and health-probe failures: a
command that succeeds while doing nothing.

### 11.5 Terraform — evaluated, not built

The Azure plane is the only layer with no code at all; it exists as prose in §8. Terraform was
sketched to see what it would actually buy.

**The highest-value piece is not resource creation — it is the identity wiring.** The federated
credential's subject is currently a hand-typed
`system:serviceaccount:external-secrets:external-secrets` that must agree with a namespace and
ServiceAccount declared elsewhere, and the identity's client ID is pasted into `eso-values.yml`. In
Terraform both derive from one `locals` block, so a rename cannot silently break authentication.

Beyond that: `terraform plan` answers "has anyone clicked something in the portal", `destroy`/`apply`
makes the from-zero claim testable rather than asserted, and the bootstrap paradox of §8 shrinks from
two manual commands to one `terraform apply` — because the root Application becomes a resource.

**The boundary matters more than the coverage.** Terraform owns the cluster and the thing that
manages the cluster; ArgoCD owns what runs on it. Terraform stops immediately after applying
`root.yaml` and never touches `helm/iep`. Crossing that line gives two controllers reconciling one
object and permanent drift in `terraform plan`.

**What would deliberately stay manual:**

- **Key Vault secret values.** An `azurerm_key_vault_secret` resource puts the value in plaintext in
  state. Creating the vault and its RBAC in code while entering values by hand is what preserves the
  "no stored credential" posture — automating it would make the security worse.
- **The state backend.** The storage account holding state cannot be created by the config that uses
  it. The bootstrap paradox, one level up.
- **GHCR package visibility** — the cause of the `ImagePullBackOff` in §4 — is poorly covered by the
  GitHub provider, and the package does not exist until the first push.
- `kubernetes_manifest` runs a server-side dry-run at *plan* time, so it fails against a cluster that
  does not exist yet. A config like this needs a two-phase apply or the `kubectl` provider.

Not implemented. Recorded because the interesting result is the **boundary**, not the code.

### 11.6 What the chart became

The clearest way to state the architectural shift:

**The chart stopped being the deployment unit and became the source of truth for what the deployment
looks like.** On this cluster it is a template engine CI invokes; the artifact that ships is rendered
YAML on a branch. For anyone else it is still an ordinary installable chart — `values.yaml` bundles
ingress-nginx, creates its own Secret, and needs only `--set registry=ghcr.io/rsgrbic` to run
elsewhere, since CI publishes a `:latest` tag alongside each SHA.

That split explains the drift between the two values files. They are no longer two environments of
one install path; they are two different kinds of artifact — a chart's defaults, and CI input.

A consequence worth stating: `helm list -n iep` still reports a release at revision 7 from before
ArgoCD took over. Nothing acts on it. It is a fossil that actively misleads, and it is worth clearing
precisely because the point of this architecture is that "what put this here" has one obvious answer:

```powershell
kubectl delete secret -n iep -l owner=helm
```

### 11.7 Still open after this section

- `iep` remains on **manual sync**; only `root` is automated. Self-heal has never been demonstrated.
- `argocd/apps/cert-manager.yaml` ignores only `ValidatingWebhookConfiguration`. cert-manager v1.21.1
  also ships a `MutatingWebhookConfiguration` that `admissionsenforcer` mutates identically — this
  will reconcile-loop the moment automated sync is enabled.
- Neither add-on Application carries `CreateNamespace=true`, so a genuine from-zero install deadlocks
  at sync-wave −1 until something creates the `iep` namespace. The reproduction steps in §8 have
  therefore **not been executed end to end**.
- `kubernetes/` still exists alongside the chart — two descriptions of one cluster.
- The application-level findings in §11.1 are recorded and unfixed by choice.

### 11.8 Two more transferable lessons

7. **A pipeline that writes to the branch it is triggered by has a concurrency bug waiting.** The
   fix is not better locking; it is removing the second writer. Rendering to a separate branch means
   CI and humans never touch the same lines.
8. **Rendering makes implicit behaviour checkable.** "director never scales past one replica" was a
   comment and an assumption until three rendered files made it a thing that could be asserted
   across the whole configuration matrix.

## 12. Stage 5 — making the system observable

Everything above this section describes a cluster whose health was a binary: pods were Running or
they were not. Nothing recorded *how* the system behaved — how long a blockchain vote took, whether
votes were timing out, whether the Redis order queue was draining. This section is the work that
changed that, and it turned out to be less about Prometheus than about the ways a monitoring system
can look healthy while measuring nothing.

The framing that made it worth doing: **§11.1 identified failure modes that were never fixed.
Metrics turn each of them into something watchable.** The vote-timeout counter measures the
event-filter race. The pending-orders gauge measures the Redis leak. The live-threads gauge measures
the exact constraint that pins `director` to one replica. That is observability as verification of
known failure modes, rather than graphs for their own sake.

### 12.1 The stack, and a value that fails silently

`kube-prometheus-stack` 88.5.0 as its own ArgoCD Application at sync-wave `-1`, so its
`ServiceMonitor` and `PrometheusRule` CRDs exist before the iep chart renders objects of those
kinds. The same ordering problem cert-manager's CRDs already solved, with the same solution.

Most of the values turn something off. AKS manages the control plane, so `kubeControllerManager`,
`kubeScheduler`, `kubeEtcd` and `kubeProxy` are not reachable; left enabled they become permanently
down targets that fire their default alerts forever. Retention is capped at 7d and 8GB, because
Prometheus with default retention on a single node fills the disk and gets OOMKilled, which is a bad
way to learn this.

One value matters more than the rest:

```yaml
serviceMonitorSelectorNilUsesHelmValues: false
```

The default is `true`, which restricts the operator to ServiceMonitors carrying its own release
label. Our monitors come from the iep chart and carry iep labels, so under the default they are
ignored — with no error, no event, and no log line. The object applies cleanly, ArgoCD reports
Synced and Healthy, Prometheus starts and stays green, and the target simply never appears. This is
the same failure signature as the `.helmignore` and health-probe problems in earlier sections:
**silent success**.

The first sync reported `Failed` while every pod was Running and healthy. The failed task was a
single resource — the `Prometheus` CR — with `hookPhase: Failed` but `status: Synced` and the
message `serverside-applied`. The apply had worked; ArgoCD then waited for the CR to report healthy
and the StatefulSet's PVC was still binding when the wait expired. Nothing needed re-syncing. Worth
recording because "sync failed, everything is fine" is an alarming combination to meet for the first
time.

One prediction that did not survive contact. I expected the stack's admission webhooks to drift,
because a `kube-webhook-certgen` Job patches a `caBundle` into them at runtime that git will never
contain — the same class of problem as §6's AKS/ArgoCD fight. It did not happen: the `caBundle`
stayed empty, which is exactly what git says, so there was nothing to diverge. The side effect is
worth knowing though — those webhooks have `failurePolicy: Ignore` and no CA, so **PrometheusRule
validation is inert**. A malformed alert rule is accepted and then quietly ignored by the operator
rather than rejected at apply time.

A correction to my own planning: I wrote that AKS makes `apiserver_*` metrics largely unavailable.
That is wrong. The chart renders a `kube-api-server` ServiceMonitor, the `kubernetes` Service exists
on AKS, and the target comes up. Only etcd, the scheduler and the controller-manager are hidden.

### 12.2 Instrumenting the application, and a counter that does not exist

`prometheus-flask-exporter` in all three services gives RED per endpoint for two lines of code. The
custom metrics are the interesting part: `iep_voting_threads_active`, `iep_voting_duration_seconds`,
`iep_voting_outcome_total{outcome}`, `iep_pending_orders`, `iep_assets_value{category,kind}`, and
`iep_login_total{result}`.

Two facts turned out to be load-bearing. **gunicorn runs one worker per pod** — no `--workers` flag
in any Dockerfile — so metrics live in a single process registry and need no
`PROMETHEUS_MULTIPROC_DIR`. Adding `--workers` later would silently break every gauge, because each
scrape would hit a random worker. And **there are no path parameters** in any of the twelve routes,
so the `path` label is bounded at four values per service and cardinality is not a risk.

`iep_voting_threads_active` is the one worth keeping. `/decision` starts a thread and returns 200
immediately, so no HTTP metric can see the work at all. The thread can live for up to
`VOTING_DEADLINE_SECONDS` — an hour by default — inside a single Python process. That is precisely
why `director` is pinned to `replicaCount: 1`. Before this the constraint was a comment in
`values.yaml`; now it is a number on a graph.

Then the finding that justifies the whole approach of checking before writing.

Before writing any alert I queried Prometheus for the series count of every metric an alert would
depend on. `iep_voting_outcome_total` came back with **zero series**. A labelled Counter in
`prometheus_client` creates no child series until its first `.inc()`, and no vote had been cast
since deploy. The consequence is not that the metric reads zero. It is that `increase()` over a
window containing no series returns *nothing at all* — not `0`. An alert on it would have evaluated
to empty for ever, and empty looks exactly like "no timeouts have occurred".

The fix is three lines at module scope:

```python
for _outcome in ("approved", "rejected", "timeout", "filter_error"):
    VOTING_OUTCOME.labels(outcome=_outcome)
```

Calling `.labels()` without `.inc()` creates the series at zero. This is the single most important
thing learned in this stage: **an alert on a metric that has never been emitted is not quiet, it is
not evaluated**, and there is no error anywhere to tell you.

While instrumenting, the auth bypass from §11.1 was finally fixed. The old line read
`if role is None and not director_role == role`, which is worse than the review described: with
`role: "employee"` the first term is false, the whole condition is false, and the request passes. A
valid employee token reached `/report`, `/pending_orders` and `/decision`. Only a token with no
`role` claim at all was rejected, and that by accident. It is now two explicit checks.

### 12.3 Attribution: three libraries, three different hooks

The thesis requirement asks for "metrics on each service's usage of mongo, redis". Exporters cannot
answer that — from MongoDB's side every connection looks the same. Only the client can attribute a
call to a caller.

Each library exposes that differently, and the differences are instructive:

- **pymongo** has an official observer interface. Implement `monitoring.CommandListener` with
  `started`/`succeeded`/`failed` and register the instance process-wide.
- **SQLAlchemy** has an event system. Attach a function to a named event on a target with
  `event.listens_for(Engine, "before_cursor_execute")`. The signature is fixed per event and a
  mismatch fails at call time, not at registration — a database query failing because of a
  monitoring function.
- **redis-py** has neither. Every command funnels through `execute_command`, so the answer is a
  subclass overriding that one method. Not a hook at all.

The resulting `iep_db_operations_total{backend,operation,result}` counts **wire commands, not
logical calls**, which was confirmed against the live cluster: `count_documents()` sends
`aggregate`, and `scan_iter` over sixteen keys sent two `SCAN` commands. That is the right unit,
because it is the same unit the server counts — which is what makes the cross-check meaningful.

Attribution is free at query time: the ServiceMonitor labels every series with `job`, so
`sum by (job) (rate(iep_db_operations_total{backend="mongo"}[5m]))` answers the question without any
label of our own.

The unit tests broke, and the reason was worth the interruption. `tests/unit/_loader.py`
deliberately loads two services into one Python process, and `prometheus_client` has one global
registry per process, so identical metric names collided with `DuplicateTimeseries`. In production
each service is its own container and this cannot happen. The collision is an artefact of the test
harness, so the harness is where it was handled — unregistering each module's collectors after
loading it.

### 12.4 A slash in a job id stopped CI entirely

Two commits landed on `main` and the `deploy` branch did not move. `argocd app sync iep` reported
success because there was genuinely nothing new to apply.

The cause was one character. The chart job had been renamed to `render-manifests/publish-chart`, and
GitHub Actions job IDs allow only letters, digits, `-` and `_`. A `/` makes the **entire workflow
file invalid**, so no job runs — not the render, not the image build, not even the unit tests. There
is no failed run in the Actions tab to look at, because nothing was ever scheduled.

The failure mode is the interesting part: a syntactically invalid workflow is indistinguishable from
a repository with no CI. Everything downstream reports success, because everything downstream is
comparing against a stale artefact that is itself perfectly valid.

I had flagged the invalid id in review before it was committed. It was committed anyway. Recording
that is more useful than pretending the review caught it.

### 12.5 ServiceMonitor cannot see a port no Service publishes

The ArgoCD ServiceMonitor produced zero targets while reporting no error. The object existed, the
selector was well-formed, and nothing matched.

A ServiceMonitor does not scrape a Service. The operator selects Services, resolves their
**Endpoints**, and scrapes the pods behind them. The consequence is absolute: **it can only reach a
port that a Service publishes.** A container port absent from every Service spec is invisible to it,
whatever the selector says.

ArgoCD here was installed with the argo-cd **Helm chart**, which gates its metrics Services behind
`<component>.metrics.enabled`, default `false`. The raw `install.yaml` ships `argocd-metrics` and
`argocd-server-metrics` unconditionally — and the raw manifests are what I checked the selector
against. Verifying against upstream documentation instead of against the actual cluster is the whole
mistake, and it is one this document has now recorded twice.

The containers do expose the ports, all named `metrics`. So the fix is a **PodMonitor**, which
selects pods by label and scrapes a named container port with no Service involved. One object covers
six components, because every pod carries `app.kubernetes.io/part-of: argocd`.

One target still refused: `argocd-dex-server` on `:5558`. Dex declares a metrics port and never
binds it — it serves telemetry only when its config contains a `telemetry.http` section, and
`argocd-cm` has no `dex.config` at all, so no SSO connector is configured and dex runs idle. Excluded
with a `NotIn` expression rather than left permanently red, because **a target that can never come
up trains you to ignore red**.

### 12.6 Three exporters planned, one kept

The plan called for `mysqld-exporter`, `mongodb-exporter` and `redis-exporter`, plus a custom sidecar
for Ganache since none exists. All four were built or specified. Three were then cut, and the reason
is worth recording because it is a planning error rather than a technical one.

Pulling client-side attribution forward into §12.3 ate most of the case for the database exporters.
Operation counts and latency are now measured **per service**, with better labels than any exporter
can produce. Volume growth is already covered by `kubelet_volume_stats_available_bytes`. What was
left for mysqld-exporter was connection-pool exhaustion on a single-replica app with one table.

`redis-exporter` survived because it reports **state**, not events, against a component that can
genuinely fail that way. Redis holds the employee-to-director order queue with AOF on, no
`maxmemory` and no eviction policy, under a 256Mi container limit. `iep_pending_orders` says the
count is rising; `redis_memory_used_bytes` says how close that is to the wall, which a count cannot,
because order size varies. It also provides `redis_db_keys` as an outside check on the app's own
count — and on the live cluster the two agreed at 16.

The Ganache exporter was dropped for a sharper reason. Its headline metric was `eth_blockNumber`
resetting, to detect that Ganache restarted and wiped the chain along with every in-flight voting
contract. But Ganache is PID 1 in its container, so the process cannot die without the container
dying, and `kube_pod_container_status_restarts_total` already records that — a metric already being
collected, from a source I had dismissed in the plan as "insurance with a low payout". The custom
exporter's best metric duplicated a free one.

### 12.7 maxmemory, and getting a unit backwards twice

Redis ran with `maxmemory 0`, meaning unlimited, so memory grew until the kernel OOM-killed it. That
is an uncontrolled stop: the client gets a dropped connection rather than an error, the pod restarts
and replays the AOF while every write fails, and nothing has changed so it happens again.

With a cap and `maxmemory-policy noeviction`, Redis stays alive and refuses writes with
`OOM command not allowed`. The important half is that **reads still work**, so `director` keeps
draining the queue while `employee` is blocked from adding to it. Keys are deleted, memory falls,
writes resume. That is backpressure, and the system recovers by itself. An OOM-kill stops the
consumer too, so nothing drains and nothing recovers.

`noeviction` is not a tuning choice here. Six of the eight eviction policies delete data, and this
data is pending orders. An evicted key is a lost order with no record that it existed.

Then the embarrassing part. I set `150mb`, then "corrected" it to `128mib` on the claim that Redis
reads `mb` as 10⁶ bytes and `mib` as 2²⁰. Both halves were wrong. Redis has **no `mib` suffix at
all**, and its two-letter suffixes are the binary ones — `mb` is 1024×1024, matching Kubernetes
`Mi` exactly. The pod refused to start:

```
*** FATAL CONFIG FILE ERROR (Redis 7.4.11) ***
>>> 'maxmemory "128mib"'
argument must be a memory value
```

The original value had been fine. The lesson is narrow but real: a confident correction is still a
change, and this one was shipped without being run.

The 50% figure is also worth being honest about — it is the conventional rule of thumb for a Redis
that forks, not a calculation. `maxmemory` bounds the dataset, while fragmentation and the
copy-on-write fork during an AOF rewrite sit outside it and cannot be predicted before measuring.
The measurement that should eventually replace the guess is
`container_memory_working_set_bytes / redis_memory_used_bytes`, which is the real overhead ratio and
is only available *because* the exporter exists.

### 12.8 Downsizing the node, blocked twice

The node was a `Standard_D4ds_v4` — 4 vCPU, 16 GiB — running at 370m CPU and about 5 GiB. CPU was
never the constraint. The workload requests 480m across the chart and the monitoring stack combined.

Two attempts to move to a 2 vCPU machine failed, each for a reason worth knowing.

**Quota.** `Standard EADSv5 Family vCPUs` is `0 / 0` on this subscription and the current
`DDSv4` family is `4 / 4`, fully consumed. Quota is per VM family, and this is an **Azure for
Students** subscription, which is generally not eligible for increases. The trap is that
`az vm list-skus` reports the SKU as available with `restrictions: []` — availability and quota are
different questions, and only `az vm list-usage` answers the second.

**The OS disk.** AKS had sized the ephemeral OS disk to 150 GiB to fill the D4ds_v4's resource disk.
Every 2 vCPU / 16 GiB SKU in the region caps its resource disk at 75 GiB, and Azure will not shrink
an existing OS disk. Those two facts cannot both be satisfied, so `az vmss update` has no path at
all — the error is about the disk, but the situation is unfixable rather than one flag away.

A new node pool sets the OS disk at creation, where there is nothing to shrink. That is also the
only AKS-supported route: `az aks nodepool update` has no size flag, and editing the VMSS directly
means editing a resource AKS owns and reconciles against an agent pool definition that still records
the old size.

One pricing subtlety inverted the obvious answer. The AMD `as_v5` SKUs looked cheapest, but the `a`
without a `d` means no local resource disk, so ephemeral OS disk is unsupported and the node falls
back to a managed disk at $21.68/mo. Both cheap-looking rows ended up **more expensive** than their
`d` equivalents.

The drain surfaced one more thing worth understanding. It stalled on `metrics-server` with
`Cannot evict pod as it would violate the pod's disruption budget`, and the PDB showed
`ALLOWED DISRUPTIONS 0`. The instinct is to delete the pod, which bypasses the PDB entirely. That
would have been wrong: both replacements were already on the new node and the second was 17 seconds
old and not yet ready, so the budget was correctly refusing to drop below one healthy replica. It
resolved itself within a minute. **A PDB blocking a drain is usually the PDB working.**

### 12.9 Alerts, and finding out nothing was listening

Seven rules in a `PrometheusRule` in the chart. Every expression was sent to the live Prometheus
`/api/v1/query` before being committed, which matters more here than usual because the validating
webhook is inert (§12.1) — a malformed rule is accepted and then silently ignored.

Two rules needed reshaping because of how their metrics behave rather than what they measure.

`kube_pod_container_status_last_terminated_reason` is a gauge that keeps reporting the last
termination reason **for ever**. On its own, `> 0` fires permanently after a single OOMKill. It is
paired with `increase(kube_pod_container_status_restarts_total[1h]) > 0` and joined with
`and on (namespace, pod, container)`, which bounds it to the actual event.

The queue rule uses depth *and* slope — `iep_pending_orders > 20 and deriv(iep_pending_orders[30m])
> 0`. Depth alone is meaningless: a queue of 500 that is draining is healthy, a queue of 21 that
keeps climbing is not.

Then Grafana was OOMKilled, which is how the alerting was tested without arranging a test. It sat at
**507 MiB against a 512 MiB limit** — the limit I had set from the plan's estimate of 200 MiB, which
was too low for Grafana 13 with roughly 33 provisioned dashboards.

The reason it climbed to exactly the limit is not a leak. Go's garbage collector decides when to
collect from `GOGC`, a ratio of the live heap, and knows nothing about the cgroup limit. It grows
until the kernel kills the process. Raising the limit alone would have moved the wall to 1 GiB and
nothing else. The fix that changes the behaviour is `GOMEMLIMIT=900MiB`, a soft ceiling the GC
actually respects. The request was also raised from 128Mi to 512Mi, because a request far below real
usage means the scheduler is packing the node with fiction.

And the part that matters most: **the alert fired, and nobody noticed.** Checking each side of the
expression separately showed why — the restart was more than fifteen minutes old, so the alert had
already resolved. The window was widened to an hour.

But the deeper reason is that four alerts were firing that nobody knew about at all:

```
firing  Watchdog
firing  InfoInhibitor
firing  TargetDown          job="argocd/argocd"
firing  CPUThrottlingHigh   container="mongo"
```

Alertmanager is running and has **no receiver configured**. Alerts reach it and stop. `Watchdog` and
`InfoInhibitor` are supposed to fire permanently — `Watchdog` is a dead-man's switch you route
somewhere that complains when it *stops* arriving. The other two were real: `TargetDown` was the dex
target, and `CPUThrottlingHigh` reports mongo throttled 27.76% of the time against its 1000m limit,
which nothing had ever noticed.

An alerting pipeline with no receiver is not alerting. It is a dashboard that has to be visited.

### 12.10 Dashboards as code, and three traps

Dashboards ship as ConfigMaps labelled `grafana_dashboard: "1"`. kube-prometheus-stack runs a
sidecar that watches for them, and — unlike the plain Grafana chart, which watches only its own
namespace — sets `searchNamespace: ALL`, so an object in the `iep` namespace is picked up with no
change to the monitoring Application.

The obvious question was why not simply give Grafana a PVC. It would persist UI-made dashboards, but
it solves the wrong problem: the issue is not that dashboards are volatile, it is that they are not
in git. A dashboard in a SQLite file on an Azure disk cannot be diffed or reviewed, and disappears
if the cluster is rebuilt — which the whole GitOps story claims is safe. Provisioned dashboards are
also read-only in the UI regardless, so a PVC would not even allow editing them in place.

Three traps, in the order they were hit.

**Helm and Grafana share `{{ }}`.** Grafana legend formats are written `{{outcome}}`. Put the
dashboard JSON under `templates/` and Helm tries to evaluate that. The JSON lives in
`helm/iep/dashboards/` instead, and the template pulls it in with `.Files.Get`, which returns files
outside `templates/` byte for byte.

**`increase()` extrapolates**, so a single event can display as `1.03`. Fixed with `round()` on the
query *and* `decimals: 0` on the field config — the first fixes the tooltip, the second fixes the
axis. It is also why every counter alert uses `> 0` rather than `>= 1`.

**A community dashboard is not ready to provision.** Dashboard 763 for redis-exporter ships
`__inputs: ['DS_PROM']` and 34 `${DS_PROM}` references, which only the interactive import wizard
resolves. Through the sidecar there is no wizard, so every panel would have failed to find a
datasource. The placeholder had to be replaced with the provisioned uid and the `__inputs` and
`__requires` blocks stripped.

The custom dashboards are generated from Go with the **Grafana Foundation SDK**, because a dashboard
is 10–40 kB of JSON and JSON has no comment syntax. Hand-written, a diff shows twenty changed lines
and none of them says why. In Go the queries sit next to the reasoning, and `go build` rejects a
misspelled builder method — where Grafana would accept a misspelled JSON field and silently ignore
it. Grafonnet, the older answer, is deprecated; Grafana now points at this SDK.

The SDK emits a deprecation warning recommending `dashboardv2.Dashboard`. **Do not follow it.** v2 is
a different document — a Kubernetes resource with `apiVersion`/`kind`/`spec` and an `elements` map
instead of a `panels` array — and Grafana's file provisioner, which is what the sidecar feeds,
rejects it with `dashboard appears to be in v2 format`. Using it needs the `kubernetesDashboards`
feature toggle and a different provider. The grafana-operator has the same split: its
`GrafanaDashboard` CR is v1-only and v2 needs `GrafanaManifest`. The trigger for moving is a change
of delivery mechanism, not a new SDK version. That reasoning is recorded in the package doc, because
this is exactly the kind of warning someone acts on later and breaks the delivery path.

### 12.11 Logs: the collector cannot ship what was never written

Loki stores and queries; it collects nothing. Something has to push into it, and that something used
to be Promtail, which reached end of support in March 2026 and was merged into Alloy. Fluent Bit,
Vector and any OTLP sender remain viable — Loki 3.x accepts OTLP natively — so Alloy is a choice
rather than a requirement. It wins here for two specific reasons: `loki.source.kubernetes_events`,
and relabel rules that produce `namespace`, `pod`, `container` and `app` labels **matching the names
Prometheus already uses**, which is what puts a metric graph and its logs one click apart.

Kubernetes events are the half that is easy to forget. They are API objects with roughly a one-hour
TTL, not log files, so `ImagePullBackOff` reasons and probe failures evaporate. Every incident
diagnosis in §4 depended on catching `kubectl describe` in the moment.

But the phase does not begin in the cluster. `director/app.py` had `except Exception: pass` inside
the vote-polling loop, and exactly one `app.logger` call existed in the entire codebase. Alloy ships
stdout. **If nothing is written to stdout, nothing ships** — no collector can recover an exception
that was discarded without being printed.

So the first change was structured JSON logging in all three services, and real log calls where
exceptions were being swallowed. The order UUID is carried as a field from `employee` creating the
order through `director` watching the vote, which is what makes a single vote greppable end to end.
Lines are shipped raw and parsed at query time with `| json`, so the pipeline stays dumb and a change
to the log format needs no redeploy of Alloy.

One detail that is easy to get wrong: the polling loop runs twice a second for up to an hour, so an
unthrottled log line inside that `except` would be **7,200 lines per broken vote**. It logs the first
failure immediately and then at most once a minute, carrying the running total into the timeout and
success lines so nothing is lost by not printing it.

The Loki chart needed almost every value set to turn something off — `deploymentMode: SingleBinary`,
the distributed `read`/`write`/`backend` roles explicitly zeroed because the chart renders them
anyway, memcached disabled because it asks for gigabytes, and the gateway and canary removed.
`schemaConfig` is mandatory since chart 6.x: Loki refuses to start rather than guess a schema and
silently orphan data. And `retention_period` alone only *marks* data expired — the compactor has to
be enabled or nothing is ever deleted and the disk fills.

### 12.12 Still open after this section

- **Alertmanager has no receiver.** Seven rules, and nowhere for them to go.
- **Alloy's chart grants cluster-wide read on `secrets` and `configmaps`** to a log collector. That
  covers `iep-secret`. It is the chart's blanket RBAC for components this config does not use, and
  narrowing it means supplying a hand-written ClusterRole.
- **The grader has never been run against the instrumented cluster.** Every vote panel is flat and
  `iep_voting_outcome_total` is zero across all four labels. The question the whole stage was built
  to answer — does the §11.1 event-filter race actually happen — is still unanswered.
- **`/metrics` is publicly reachable.** The ingress routes `/` to each service, so
  `https://<host>/metrics` is open. A TODO sits in the ingress template; an nginx `server-snippet`
  returning 403 is the fix.
- **mongo is CPU-throttled 27.76% of the time** against its 1000m limit. Measured, not investigated.
- **Phase 5, `prometheus-adapter`, was not built.** The chart has kept HPAs since §1 for "custom
  metrics from Prometheus", and that is still not true.

### 12.13 Transferable lessons

9. **A metric that has never been emitted is not zero — it does not exist.** `increase()` over an
   absent series returns nothing, so an alert on it is never evaluated rather than quietly false.
   Pre-create every label value you intend to alert on, and check series counts before writing rules.
10. **Verify against the cluster, not against upstream documentation.** The ArgoCD ServiceMonitor
    failed because the installation method differed from the manifests I checked. This document has
    now recorded the same mistake twice, in different sections.
11. **Silent success is the dominant failure mode in this stack.** An ignored ServiceMonitor, an
    invalid workflow file, an inert admission webhook, a dashboard field Grafana does not recognise
    — none produces an error. Every one of them reports healthy. Build the habit of asking what a
    component would look like if it were doing nothing, and check for that specifically.
12. **A confident correction is still a change.** The `128mib` unit "fix" was shipped without being
    run and stopped Redis from starting. Corrections deserve the same verification as the original.
13. **An alerting pipeline without a receiver is a dashboard.** Four alerts fired for days without
    being noticed. Routing is not the last 10% of alerting; it is what makes the rest of it real.
