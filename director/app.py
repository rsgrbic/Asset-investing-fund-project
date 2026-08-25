from flask import Flask, jsonify
from flask_jwt_extended import JWTManager,get_jwt,verify_jwt_in_request
from pymongo import MongoClient, monitoring
from collections import defaultdict
import os
import sys
import logging
from redis import Redis
import json
from web3 import Web3
import uuid as uuid_lib
from flask import request

from datetime import datetime,timezone
import threading
import time

from bson import ObjectId
from bson.errors import InvalidId


# ---- structured logging ------------------------------------------------------
# One JSON object per line on stdout. Loki stores lines verbatim, so the format
# only matters at query time: `| json` then turns every key below into a
# filterable label without a regex.
_LOG_STD_ATTRS = set(
    vars(logging.LogRecord("", 0, "", 0, "", (), None))
) | {"message", "asctime", "taskName"}


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed as logger.info("...", extra={"order_uuid": x}) lands
        # on the record as a plain attribute. Emit whatever is not standard.
        for key, value in record.__dict__.items():
            if key not in _LOG_STD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configure_logging():
    """Replace the root handlers so gunicorn's default format does not win."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


_configure_logging()
log = logging.getLogger("iep.director")

from prometheus_client import Counter, Gauge, Histogram
from prometheus_flask_exporter import PrometheusMetrics

# Module scope, not create_app(): a second create_app() call would otherwise
# register the same metric name twice and raise.
VOTING_THREADS = Gauge(
    "iep_voting_threads_active",
    "Vote watcher threads alive in this process",
)
# A vote runs until VOTING_DEADLINE_SECONDS (3600).
VOTING_DURATION = Histogram(
    "iep_voting_duration_seconds",
    "Seconds from vote start to the Finalized event",
    buckets=(1, 5, 15, 30, 60, 120, 300, 900, 3600),
)
VOTING_OUTCOME = Counter(
    "iep_voting_outcome_total",
    "How a vote ended",
    ["outcome"],
)
# Create the four child series at import. A labelled Counter has no series
# until its first .inc(), and increase() over a window with no data returns
# nothing at all -- not 0. An alert on a metric that does not exist never
# fires and never shows an error, so the silence looks like health.
for _outcome in ("approved", "rejected", "timeout", "filter_error"):
    VOTING_OUTCOME.labels(outcome=_outcome)
PENDING_ORDERS = Gauge("iep_pending_orders", "Orders waiting in Redis")
ASSETS_VALUE = Gauge("iep_assets_value", "Money per category", ["category", "kind"])

DB_OPS = Counter(
    "iep_db_operations_total",
    "Database calls made by this service",
    ["backend", "operation", "result"],
)
DB_DURATION = Histogram(
    "iep_db_operation_duration_seconds",
    "Database call latency",
    ["backend", "operation"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5),
)


class _MongoMetrics(monitoring.CommandListener):
    """Tracks Mongo commands. pymongo provides a base class."""

    def started(self, event):
        pass

    def succeeded(self, event):
        DB_OPS.labels("mongo", event.command_name, "ok").inc()
        DB_DURATION.labels("mongo", event.command_name).observe(event.duration_micros / 1e6)

    def failed(self, event):
        DB_OPS.labels("mongo", event.command_name, "error").inc()


# Process-wide, so every MongoClient is covered. Registered at module scope:
# a second create_app() call would otherwise add a second listener and the
# counts would double.
monitoring.register(_MongoMetrics())


class _MeteredRedis(Redis):
    """Superclass that wraps Redis client to measure operations."""

    def execute_command(self, *args, **kwargs):
        op = args[0] if args else "unknown"
        start = time.perf_counter()
        try:
            result = super().execute_command(*args, **kwargs)
        except Exception:
            DB_OPS.labels("redis", op, "error").inc()
            raise
        DB_OPS.labels("redis", op, "ok").inc()
        DB_DURATION.labels("redis", op).observe(time.perf_counter() - start)
        return result

def _is_valid_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        uuid_lib.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _is_eth_address(value):
    if not isinstance(value, str):
        return False
    return Web3.is_address(value)

def create_app():
    
    
    def _require_director():
        try:
            verify_jwt_in_request()
        except Exception:
            return jsonify({"msg":"Missing Authorization Header"}), 401
        
        claims = get_jwt()
        director_role = os.getenv("DIRECTOR_ROLE", "director")
        role = claims.get("role")
        if role is None:
            return jsonify({"msg":"Missing Authorization Header"}), 401
        if role != director_role:
            return jsonify({"msg":"Missing Authorization Header"}), 401
        return None
    
    def _format_tx(tx):
        """Convert web3 dict to normal dict"""
        out = {}
        for key, value in tx.items():
            if isinstance(value, bytes):
                out[key] = "0x" + value.hex()
            else:
                out[key] = value
        return out
    
    def _deploy_voting_contract(voters):
        """Deploy a new Voting contract and return the address plus two pre-built txs."""
        contract = web3_client.eth.contract(
            bytecode=contract_artifact["bytecode"],
            abi=contract_artifact["abi"],
        )
        accounts = web3_client.eth.accounts
        if not accounts:
            raise RuntimeError("no accounts available on ganache")
        deployer = accounts[0]
        checksum_voters = [Web3.to_checksum_address(v) for v in voters]

        tx_hash = contract.constructor(checksum_voters).transact({
            "from": deployer,
            "gas": 3_000_000,
        })
        receipt = web3_client.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        contract_address = receipt["contractAddress"]

        instance = web3_client.eth.contract(
            address=contract_address,
            abi=contract_artifact["abi"],
        )
        sample_voter = checksum_voters[0]

        approve_tx = instance.functions.castApprove().build_transaction({
            "from": sample_voter,
            "nonce": 0,           # placeholder — the real voter overwrites this
            "gas": 3_000_000,
            "gasPrice": web3_client.eth.gas_price,
        })
        reject_tx = instance.functions.castReject().build_transaction({
            "from": sample_voter,
            "nonce": 0,
            "gas": 3_000_000,
            "gasPrice": web3_client.eth.gas_price,
        })

        return {
            "contract_address": contract_address,
            "approve_transaction": _format_tx(approve_tx),
            "reject_transaction": _format_tx(reject_tx),
        }
    
    def _finalize_order(order_uuid,approved):
        rawData = redis_client.get(f"{PENDING_ORDER_PREFIX}{order_uuid}")
        if rawData is None:
            return
        try:
            order=json.loads(rawData)
        except (ValueError,TypeError):
            redis_client.delete(f"{PENDING_ORDER_PREFIX}{order_uuid}")
            return
        if not approved:
            redis_client.delete(f"{PENDING_ORDER_PREFIX}{order_uuid}")
            return
        
        order_type= order.get("order_type")
        if order_type== "BUY":
            assets_collection.insert_one({
                "name": order.get("name"),
                "categories": order.get("categories", []),
                "buying_date": _now_iso(),
                "buying_price": order.get("buying_price"),
                "info": order.get("info", {})
            })
        elif order_type=="SELL":
            try:
                oid = ObjectId(order.get("id"))
            except (InvalidId,TypeError):
                redis_client.delete(f"{PENDING_ORDER_PREFIX}{order_uuid}")
                return
            assets_collection.update_one(
                {"_id":oid},
                {"$set":{
                    "selling_date": _now_iso(),
                    "selling_price": order.get("selling_price")
                }}
            )
        
        redis_client.delete(f"{PENDING_ORDER_PREFIX}{order_uuid}")
        return
    
    def _catch_voting(order_uuid,contract_address):
        instace= web3_client.eth.contract(
            address=Web3.to_checksum_address(contract_address),
            abi=contract_artifact["abi"]
        )
        try:
            event_filter = instace.events.Finalized.create_filter(fromBlock="latest")
        except Exception:
            VOTING_OUTCOME.labels(outcome="filter_error").inc()
            log.exception(
                "vote watcher could not attach to the contract",
                extra={"order_uuid": order_uuid, "contract": contract_address},
            )
            return

        deadline = time.time() + int(os.environ.get("VOTING_DEADLINE_SECONDS",3600)) # 1 Hour to finalzie
        started = time.time()
        poll_errors = 0
        last_error_logged = 0.0
        log.info(
            "vote watcher started",
            extra={
                "order_uuid": order_uuid,
                "contract": contract_address,
                "deadline_seconds": int(deadline - started),
            },
        )
        # try/finally, so a thread that dies early still releases the gauge.
        VOTING_THREADS.inc()
        try:
            while time.time() < deadline:
                try:
                    for event in event_filter.get_new_entries():
                        approved = bool(event["args"]["approved"])
                        _finalize_order(order_uuid, approved)
                        elapsed = time.time() - started
                        VOTING_DURATION.observe(elapsed)
                        VOTING_OUTCOME.labels(
                            outcome="approved" if approved else "rejected"
                        ).inc()
                        log.info(
                            "vote finalized",
                            extra={
                                "order_uuid": order_uuid,
                                "approved": approved,
                                "duration_seconds": round(elapsed, 2),
                                "poll_errors": poll_errors,
                            },
                        )
                        return
                except Exception as exc:
                    # This was a bare `pass`. Swallowing is still right -- one
                    # failed read must not abandon the vote -- but it must not
                    # be silent. Throttled to one line a minute: the loop runs
                    # twice a second for up to an hour, so an unthrottled log
                    # would be 7200 lines per broken vote.
                    poll_errors += 1
                    now = time.time()
                    if poll_errors == 1 or now - last_error_logged >= 60:
                        last_error_logged = now
                        log.warning(
                            "vote poll failed, still retrying",
                            extra={
                                "order_uuid": order_uuid,
                                "contract": contract_address,
                                "poll_errors": poll_errors,
                                "error": repr(exc),
                            },
                        )
                time.sleep(0.5)
            # Reached only when the deadline passed with no Finalized event.
            VOTING_OUTCOME.labels(outcome="timeout").inc()
            log.error(
                "vote expired with no Finalized event",
                extra={
                    "order_uuid": order_uuid,
                    "contract": contract_address,
                    "waited_seconds": round(time.time() - started),
                    "poll_errors": poll_errors,
                },
            )
        finally:
            VOTING_THREADS.dec()
    
    # Locals
    
    app = Flask(__name__)
    app.config["JWT_SECRET_KEY"]= os.environ.get("JWT_SECRET_KEY","HARDCODED")
    JWTManager(app)

    # Adds /metrics and RED metrics per route.
    PrometheusMetrics(app)
    
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    mongo_client = MongoClient(mongo_url)
    assets_collection= mongo_client["investment_fund"]["assets"]
    
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    redis_client = _MeteredRedis.from_url(redis_url, decode_responses=True)
    PENDING_ORDER_PREFIX = "pending_order:"

    _pending_last = {"value": 0.0}

    def _count_pending_orders():
        """"Counts pending orders in redis once /metrics is called."""
        try:
            _pending_last["value"] = float(
                sum(1 for _ in redis_client.scan_iter(match=f"{PENDING_ORDER_PREFIX}*"))
            )
        except Exception as exc:
            log.warning("redis scan for pending orders failed", extra={"error": repr(exc)})
        return _pending_last["value"]

    PENDING_ORDERS.set_function(_count_pending_orders)
    
    ganache_url= os.environ.get("GANACHE_URL","http://ganache:8545") #EDIT
    web3_client= Web3(Web3.HTTPProvider(ganache_url,request_kwargs={"timeout":5}))
    CONTRACT_PATH = os.environ.get("VOTING_CONTRACT_JSON"
    ,os.path.join(os.path.dirname(__file__), "Voting.json"))
    with open(CONTRACT_PATH) as f:
        contract_artifact = json.load(f)
    
    @app.get("/report")
    def report():
        err = _require_director()
        if err is not None:
            return err
        
        spent = defaultdict(int)
        earned= defaultdict(int)
        
        for asset in assets_collection.find():
            categories= asset.get("categories",[])
            try:
                buying_price= int(asset.get("buying_price") or 0)
            except (ValueError,TypeError):
                return jsonify({"Message": "Buying price is not a number"}),501
            selling_price = asset.get("selling_price") # Can be null, no conversion
            
            for category in categories:
                spent[category]+=buying_price
                if selling_price is not None:
                    earned[category]+=int(selling_price)
            
        all_categories= set(spent.keys()).union(earned.keys())
        statistics =[
            {
                "category": category,
                "spent": spent[category],
                "earned": earned[category]
            }
            for category in all_categories
        ]
        statistics.sort(key= lambda sort: (-sort["earned"],sort["spent"],sort["category"]))

        # clear() first: a category that disappears would otherwise keep
        # reporting its last value forever.
        ASSETS_VALUE.clear()
        for row in statistics:
            ASSETS_VALUE.labels(category=row["category"], kind="spent").set(row["spent"])
            ASSETS_VALUE.labels(category=row["category"], kind="earned").set(row["earned"])

        return jsonify({"statistics":statistics}),200
    
    @app.get("/health")
    def health():
        if not web3_client.is_connected():
            return jsonify({"status":"ganache_down"}), 503
        return jsonify({"status":"ok"}),200    

    @app.get("/pending_orders")
    def pending_orders():
        err=_require_director()
        if err is not None:
            return err
        orders=[]
        for key in redis_client.scan_iter(match=f"{PENDING_ORDER_PREFIX}*"):
            data = redis_client.get(key)
            if data is None:
                continue
            
            try:
                order= json.loads(data)
            except(ValueError,TypeError):
                continue
            order_uuid = key.split(":",1)[1]
            formatted = {"uuid": order_uuid, "order_type": order.get("order_type")}
            
            if order.get("order_type") == "BUY":
                formatted["name"] = order.get("name")
                formatted["categories"] = order.get("categories", [])
                formatted["info"] = order.get("info", {})
                formatted["buying_price"] = order.get("buying_price")
            elif order.get("order_type") == "SELL":
                formatted["id"] = order.get("id")
                formatted["selling_price"] = order.get("selling_price")
            orders.append(formatted)

        return jsonify({"orders":orders}),200
        
    @app.post("/decision")
    def decision():
        err = _require_director()
        if err is not None:
            return err

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            data = {}

        uuid_value = data.get("uuid")
        if not isinstance(uuid_value, str) or len(uuid_value) == 0:
            return jsonify({"message": "Field uuid is missing."}), 400

        if not _is_valid_uuid(uuid_value):
            return jsonify({"message": "Invalid uuid."}), 400
        if redis_client.get(f"{PENDING_ORDER_PREFIX}{uuid_value}") is None:
            return jsonify({"message": "Invalid uuid."}), 400

        voters = data.get("voters")
        if not isinstance(voters, list) or len(voters) == 0:
            return jsonify({"message": "Field voters is missing."}), 400

        for v in voters:
            if not _is_eth_address(v):
                return jsonify({"message": "Invalid voter address."}), 400

        if len(voters) % 2 == 0:
            return jsonify({"message": "Even number of voters."}), 400

        voting_contract= _deploy_voting_contract(voters)
        
        threading.Thread(
            target=_catch_voting,
            args=(uuid_value, voting_contract["contract_address"]),
            daemon=True
        ).start()
        
        return jsonify({"approve_transaction": voting_contract["approve_transaction"],
                        "reject_transaction": voting_contract["reject_transaction"]}), 200
    
    return app

if __name__=="__main__":
    app= create_app()
    app.run(host="0.0.0.0",port=5002)