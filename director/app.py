from flask import Flask, jsonify
from flask_jwt_extended import JWTManager,get_jwt,verify_jwt_in_request
from pymongo import MongoClient
from collections import defaultdict
import os
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
        
        claims= get_jwt()
        director_role= os.getenv("DIRECTOR_ROLE","director")        
        role= claims.get("role", None)
        if role is not None and not director_role == role:
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
            return
        
        deadline = time.time() + int(os.environ.get("VOTING_DEADLINE_SECONDS",3600)) # 1 Hour to finalzie
        while time.time() < deadline:
            try:
                for event in event_filter.get_new_entries():
                    _finalize_order(order_uuid, bool(event["args"]["approved"]))
                    return
            except Exception:
                pass
            time.sleep(10)
    
    # Locals
    
    app = Flask(__name__)
    app.config["JWT_SECRET_KEY"]= os.environ.get("JWT_SECRET_KEY","HARDCODED")
    JWTManager(app)
    
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    mongo_client = MongoClient(mongo_url)
    assets_collection= mongo_client["investment_fund"]["assets"]
    
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    redis_client = Redis.from_url(redis_url, decode_responses=True)
    PENDING_ORDER_PREFIX = "pending_order:"
    
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