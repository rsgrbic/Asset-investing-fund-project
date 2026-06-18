from __future__ import annotations

from datetime import timedelta
import os
from email_validator import EmailNotValidError, validate_email
from flask import Flask, jsonify, request
from sqlalchemy.exc import IntegrityError
from flask_jwt_extended import JWTManager, create_access_token, get_jwt_identity, verify_jwt_in_request

from models import User ,db, hash_password, verify_password

basedir= os.path.abspath(os.path.dirname(__file__))
def _is_valid_email(email):
    try:
        validate_email(email, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False

def _is_valid_password(password):
    return 8 <= len(password) <= 256

def _get_str(data, name):
    if not isinstance(data, dict):
        return None
    value = data.get(name)
    if not isinstance(value, str) or len(value) == 0:
        return None
    return value

def create_app():

    def _seed_director():
        director_role= app.config["DIRECTOR_ROLE"]
        if User.query.filter_by(email="onlymoney@gmail.com").first() is not None:
            return
        user = User(
        email="onlymoney@gmail.com",
        forename="Scrooge",
        surname="McDuck",
        password_hash=hash_password("evenmoremoney"),
        roles=director_role
        )
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()

    app = Flask(__name__)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL",  f"sqlite:///{os.path.join(basedir, 'app.db')}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"]= False

    app.config["DIRECTOR_ROLE"] = os.environ.get("DIRECTOR_ROLE", "director")
    app.config["EMPLOYEE_ROLE"] = os.environ.get("EMPLOYEE_ROLE", "employee")

    app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY", "JWT_SECRET_DEV_KEY")
    app.config["JWT_ALGORITHM"] = "HS256"
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=1)
    JWTManager(app)

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _seed_director()

    @app.post("/register")
    def register():
        data= request.get_json(silent=True)
        # Validation
        forename = _get_str(data, "forename")
        if forename is None:
            return jsonify({"message": "Field forename is missing."}), 400
        
        surname = _get_str(data, "surname")
        if surname is None:
            return jsonify({"message": "Field surname is missing."}), 400
        
        email = _get_str(data, "email")
        if email is None:
            return jsonify({"message": "Field email is missing."}), 400
        
        password = _get_str(data, "password")
        if password is None:
            return jsonify({"message": "Field password is missing."}), 400


        if not _is_valid_email(email):
            return jsonify({"message":"Invalid email."}),400
        
        if not _is_valid_password(password):
            return jsonify({"message":"Invalid password."}), 400

        if User.query.filter_by(email=email).first() is not None:
            return jsonify({"message": "Email already exists."}), 400
        
        newUserRole= app.config["EMPLOYEE_ROLE"]
        user = User(
            email=email,
            forename=forename,
            surname=surname,
            password_hash=hash_password(password),
            roles=newUserRole,
        )
        db.session.add(user)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"message":"Internal error, try again"}), 500
        
        return "",200


    @app.post("/login")
    def login():
        data= request.get_json(silent=True)

        email= _get_str(data,"email")
        if email is None:
            return jsonify({"message":"Field email is missing"}),400

        password = _get_str(data, "password")
        if password is None:
            return jsonify({"message": "Field password is missing."}), 400

        if not _is_valid_email(email):
            return jsonify({"message": "Invalid email."}), 400
        
        user = User.query.filter_by(email=email).first()
        if user is None or not verify_password(password,user.password_hash):
            return jsonify ({"message":"Invalid credentials"}), 400
        
        additional_claims ={
            "forename":user.forename,
            "surname":user.surname,
            "roles": user.role_list()
        }

        token = create_access_token(identity=email,additional_claims=additional_claims)
        return jsonify({"accessToken":token}), 200

    @app.post("/delete")
    def delete():
        try:
            verify_jwt_in_request()
        except Exception:
            return jsonify({"msg":"Missing Authorization Header"}), 401

        identity= get_jwt_identity()
        user = User.query.filter_by(email=identity).first()

        if user is None:
            return jsonify({"message":"Unknown user."}),400
        
        db.session.delete(user)
        db.session.commit()
        return "",200



    @app.get("/health")
    def health():
        return jsonify({"status":"ok"}),200

    @app.errorhandler(422)
    def _handle_unprocessable(_err):
        return jsonify({"msg":"Missing Authorization Header"}), 401
    
    return app

if __name__=="__main__":
    port = int(os.environ.get("PORT",5000))
    app = create_app()
    app.run(host="0.0.0.0",port=port)
    
    
    