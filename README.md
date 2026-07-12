# IEP Moj — Investment Fund Portfolio Manager

A learning project that orcherstrates three Flask microservices into a complete investment workflow: registration, order management, director approval (with  blockchain voting), asset search, and data collection reports.

## Skills demonstrated

| Area | Technniques used|
|---|---|
| **Python/Flask** | Three modular Flask services, app factory pattern, gunicorn |
| **Databases** | MySQL (SQLAlchemy), MongoDB (PyMongo), Redis (order queue) |
| **Auth** | JWT (Flask-JWT-Extended), PBKDF2 password hashing, role-based guards |
| **Blockchain** | Solidity voting contract, Web3.py, Ganache local testnet |
| **Infra** | Docker Compose, Kubernetes with kubeAdm, multi-replica deployments |

## Architecture

```
auth:5000 ──> MySQL     (user registration, JWT login)
employee:5001 ──> MongoDB, Redis   (asset search, buy/sell orders)
director:5002 ──> MongoDB, Redis, Ganache  (pending orders, approval, report)
```

Orders flow through Redis: employee creates → director approves (→ optional chain: Solidity voting contract) → finalized in MongoDB.

## Complex endpoints

**`POST /employee/search`** — Dynamic MongoDB query builder supporting 15 operators (`eq`, `ne`, `gt`, `regex`, `contains`, `exists`, `size`, `all`, `in`, `nin` …), nested field dot-notation (`info.geo.country`), and combined date/category/name filters. All built from a single JSON body.

**`POST /director/decision`** — Dual-path approval endpoint. In blockchain mode, it deploys a Solidity Voting contract to Ganache, returns two pre-built unsigned Ethereum transactions (approve/reject), and spawns a background daemon thread that listens for the `Finalized` event before committing to MongoDB. In simple mode, it accepts `{"uuid": …, "approved": bool}` and processes synchronously. Touches Redis, MongoDB, and Ethereum in a single request.

**`GET /director/report`** — MongoDB aggregation that groups assets by category, sums `buying_price` (spent) and `selling_price` (earned), and returns sorted statistics.

## Quick start
Contains persistent storage, services that expose to localhost, secrets and config files.
To change ENV Vars, configure 10-configmap.yaml and 11-secret.yaml

```bash
docker build -f ./<service>/dockerfile -t iep-<service>:latest . # Repeat for all services
kubectl apply -f ./kubernetes/ #starts one pod each of auth and director, and 3 replicas of a employee app. pulls fixed images of mySql, mongoDB and ganache
```