from flask import Flask, jsonify
from flask_jwt_extended import JWTManager,get_jwt,verify_jwt_in_request
from pymongo import MongoClient
from collections import defaultdict
import os
from redis import Redis
import json
from web3 import Web3

director_role= os.getenv("DIRECTOR_ROLE","director")
 
def create_app():
    def _require_director():
        try:
            verify_jwt_in_request()
        except Exception:
            return jsonify({"msg":"Missing Authorization Header"}), 401
        
        claims= get_jwt()
        roles= claims.get("roles",[])
        if isinstance(roles, str):
            roles = [roles]
        if director_role not in roles:
            return jsonify({"msg":"Missing Authorization Header"}), 401
        return None
    
    app = Flask(__name__)
    app.config["JWT_SECRET_KEY"]= os.getenv("JWT_SECRET_KEY","JWT_SECRET_DEV_KEY")
    JWTManager(app)
    
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    mongo_client = MongoClient(mongo_url)
    assets_collection= mongo_client["investment_fund"]["assets"]
    
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis_client = Redis.from_url(redis_url, decode_responses=True)
    PENDING_ORDER_PREFIX = "pending_order:"
    
    ganache_url= os.environ.get("GANACHE_URL","http://localhost:8545")
    web3_client= Web3(Web3.HTTPProvider(ganache_url,request_kwargs={"timeout:5"}))
    CONTRACT_PATH = os.path.join(os.path.dirname(__file__), "Voting.json")
    with open(CONTRACT_PATH) as f:
        contract_artifact = json.load(f)
    
    @app.get("/report")
    def report():
        err = _require_director
        if err is not None:
            return err
        
        spent = defaultdict(int)
        earned= defaultdict(int)
        
        for asset in assets_collection.find():
            categories= asset.get("categories",[])
            buying_price= int(asset.get("buying_price") or 0)
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
        for key in redis_client.scan_iter(math=f"{PENDING_ORDER_PREFIX}*"):
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
        
    return app

if __name__=="__main__":
    app= create_app()
    app.run(host="0.0.0.0",port=5002)