from flask_sqlalchemy import SQLAlchemy
import hashlib,hmac,os


db = SQLAlchemy()

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer,primary_key=True)
    email = db.Column(db.String(256), unique=True, nullable=False, index=True)
    forename = db.Column(db.String(256), nullable=False)
    surname = db.Column(db.String(256), nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)
    roles = db.Column(db.String(256), nullable=False, default="employee")
    def role_list(self):
        return [r for r in self.roles.split(",") if r]
    
def hash_password(password: str) -> str:
    salt= os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),salt,200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False