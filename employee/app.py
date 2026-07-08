from flask import Flask, jsonify, request
import os
import re
from pymongo import MongoClient
from datetime import datetime,timezone
from flask_jwt_extended import JWTManager, verify_jwt_in_request
import uuid
from redis import Redis
import json

from bson import ObjectId
from bson.errors import InvalidId

def _make_mongo_client(url:str):
    return MongoClient(url)

def create_app():
    def _get_str(data, name):
        if not isinstance(data, dict):
            return None
        value = data.get(name)
        if not isinstance(value, str) or len(value) == 0:
            return None
        return value
    
    def _is_iso8601(value):
        if not isinstance(value,str):
            return False
        try:
            datetime.fromisoformat(value.replace("Z","+00:00"))
            return True
        except ValueError:
            return False
            
    def _to_iso_z(value:str):
        """Necessary to make everything relative to UTC. Shouldnt happen if we store information in UTC time."""
        dtime = datetime.fromisoformat(value.replace("Z","+00:00"))
        return dtime.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    def _require_employee():
        try:
            verify_jwt_in_request()
        except Exception:
            return jsonify({"msg": "Missing Authorization Header"}), 401
        
        from flask_jwt_extended import get_jwt
        claims = get_jwt()
        role = claims.get("role", None)
        employee_role= os.environ.get("EMPLOYEE_ROLE", "employee")
        if role is not None and not employee_role == role:
            return jsonify({"msg": "Missing Authorization Header"}), 401
        return None

    app = Flask(__name__)
    app.config["JWT_SECRET_KEY"]= os.environ.get("JWT_SECRET_KEY","HARDCODED")
    JWTManager(app)


    # Fallback to local mongo
    mongo_url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    mongo_client = _make_mongo_client(mongo_url)
    assets_collection = mongo_client["investment_fund"]["assets"]
    
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    redis_client = Redis.from_url(redis_url, decode_responses=True)
    PENDING_ORDER_PREFIX = "pending_order:"



    def _build_mongo_query(params):
        query={}
        name=_get_str(params,"name")
        if name is not None:
            query["name"]= {"$regex":re.escape(name),"$options":"i"}
        
        category=_get_str(params,"category")
        if category is not None:
            query["categories"]=category

        buying_date= params.get("buying_date")
        if _is_iso8601(buying_date):
            query["buying_date"]= {"$gt":_to_iso_z(buying_date)}
            
        selling_date = params.get("selling_date")
        if _is_iso8601(selling_date):
            query["selling_date"] = {"$lt":_to_iso_z(selling_date),"$exists":True,"$ne":None} # Non inclusive

        info_filters=params.get("info_filters")
        if isinstance(info_filters,list):
            for f in info_filters:
                if not isinstance(f,dict):
                    continue
                field_path= f.get("field")
                operator= f.get("operator")
                value= f.get("value")
                if not isinstance(field_path,str) or field_path=="":
                    continue
                if not isinstance(operator,str):
                    continue
                if operator == "eq":
                    query[f"info.{field_path}"] = value
                
                elif operator == "ne":
                    query[f"info.{field_path}"] = {"$ne": value}

                elif operator == "gt":
                    query[f"info.{field_path}"] = {"$gt": value}

                elif operator == "gte":
                    query[f"info.{field_path}"] = {"$gte": value}

                elif operator == "lt":
                    query[f"info.{field_path}"] = {"$lt": value}

                elif operator == "lte":
                    query[f"info.{field_path}"] = {"$lte": value}

                elif operator == "in":
                    if isinstance(value,list):
                        query[f"info.{field_path}"] = {"$in": value}
                    # value should be a list

                elif operator == "nin":
                    if isinstance(value,list):
                        query[f"info.{field_path}"] = {"$nin": value}
                    # value should be a list

                elif operator == "exists":
                    query[f"info.{field_path}"] = {"$exists": bool(value)}

                elif operator == "regex":
                    query[f"info.{field_path}"] = {
                        "$regex": value,
                        "$options": "i"
                    }

                elif operator == "contains":
                    query[f"info.{field_path}"] = {
                        "$regex": re.escape(str(value)),
                        "$options": "i"
                    }

                elif operator == "startswith":
                    query[f"info.{field_path}"] = {
                        "$regex": f"^{re.escape(str(value))}",
                        "$options": "i"
                    }

                elif operator == "endswith":
                    query[f"info.{field_path}"] = {
                        "$regex": f"{re.escape(str(value))}$",
                        "$options": "i"
                    }

                elif operator == "size":
                    if isinstance(value,int):
                        query[f"info.{field_path}"] = {"$size": int(value)}

                elif operator == "all":
                    query[f"info.{field_path}"] = {"$all": value}
                
        return query      
    
    def _format_assets_as_output(doc):
        """Convert mongo document to the described api
        id,name,categories,buying_date,buying_price,[selling_date,selling_price],info
        """
        out={
            "id": str(doc["_id"]),
            "name": doc.get("name"),
            "categories": doc.get("categories",[]),
            "buying_date": doc.get("buying_date"),
            "buying_price": doc.get("buying_price")
            }
        if doc.get("selling_date") is not None:
            out["selling_date"]= doc["selling_date"]
        if doc.get("selling_price") is not None:
            out["selling_price"]= doc["selling_price"]
        out["info"]= doc.get("info") if doc.get("info") is not None else {}
        return out;
        
    @app.post("/search")
    def search():
        err =_require_employee()
        if err is not None:
            return err
        params = request.get_json(silent=True)
        if not isinstance(params,dict):
            params={}
        query= _build_mongo_query(params)
        resList= assets_collection.find(query)
        assets= [_format_assets_as_output(r) for r in resList]
        return jsonify({"assets":assets}), 200

    @app.post("/create_buy_order")
    def create_buy_order():
        err = _require_employee()
        if err is not None:
            return err

        data = request.get_json(silent=True)

        name = _get_str(data, "name")
        if name is None:
            return jsonify({"message": "Field name is missing."}), 400

        if "categories" not in data or not isinstance(data["categories"], list):
            return jsonify({"message": "Field categories is missing."}), 400
        
        
        buying_price = data.get("buying_price")
        if buying_price is None:
            return jsonify({"message": "Field buying_price is missing."}), 400
            
        categories = data["categories"]
        if len(categories) == 0:
            return jsonify({"message": "Categories list is empty."}), 400

        if not isinstance(buying_price, (int, float)) or isinstance(buying_price, bool) or buying_price <= 0:
            return jsonify({"message": "Invalid buying price."}), 400

        info = data.get("info")
        if info is None:
            return jsonify({"message": "Field info is missing."}), 400
            
        if not isinstance(info, dict):
            info = {}

        order_uuid = str(uuid.uuid4())
        order = {
            "order_type": "BUY",
            "name": name,
            "categories": categories,
            "info": info,
            "buying_price": buying_price,
        }
        redis_client.set(f"{PENDING_ORDER_PREFIX}{order_uuid}", json.dumps(order))
        return "", 200

    @app.post("/create_sell_order")
    def create_sell_order():
        err = _require_employee()
        if err is not None:
            return err

        import json
        data = request.get_json(silent=True)

        asset_id = _get_str(data, "id")
        if asset_id is None:
            return jsonify({"message": "Field id is missing."}), 400

        selling_price = data.get("selling_price")
        if selling_price is None:
            return jsonify({"message": "Field selling_price is missing."}), 400
            
        try:
            oid = ObjectId(asset_id)
        except (InvalidId, TypeError):
            return jsonify({"message": "Invalid id."}), 400

        if assets_collection.count_documents({"_id": oid}, limit=1) == 0:
            return jsonify({"message": "Invalid id."}), 400

        if (
            not isinstance(selling_price, (int, float))
            or isinstance(selling_price, bool)
            or selling_price <= 0
        ):
            return jsonify({"message": "Invalid selling price."}), 400

        order_uuid = str(uuid.uuid4())
        order = {
            "order_type": "SELL",
            "id": asset_id,
            "selling_price": selling_price,
        }
        redis_client.set(f"{PENDING_ORDER_PREFIX}{order_uuid}", json.dumps(order))
        return "", 200
    
    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200


    @app.errorhandler(422)
    def _handle_unprocessable(_err):
        return jsonify({"msg": "Missing Authorization Header"}), 401

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app = create_app()
    app.run(host="0.0.0.0", port=port)
    
    
    