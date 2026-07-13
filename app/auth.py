"""Аутентификация и разграничение прав доступа (пп. 2.1.3.3, 2.1.3.5 ТЗ).

Доступ через веб-интерфейс без клиентских приложений; сессия — подписанная
cookie. Видимость объектов ограничивается зоной ответственности:
скважина → ГЗУ → ЦДН.
"""
import hashlib
import os

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from . import config, models
from .database import get_db

_serializer = URLSafeSerializer(config.SECRET_KEY, salt="app-session")

SESSION_COOKIE = "app_session"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return check.hex() == digest_hex


def make_session_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_token(token: str) -> int | None:
    try:
        return _serializer.loads(token).get("uid")
    except BadSignature:
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    token = request.cookies.get(SESSION_COOKIE, "")
    uid = read_session_token(token) if token else None
    user = db.get(models.User, uid) if uid else None
    if user is None or not user.is_active:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != models.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user


def visible_wells_query(db: Session, user: models.User):
    """Скважины в зоне ответственности пользователя (п. 2.1.3.5 ТЗ):
    - admin видит все скважины;
    - пользователь уровня ЦДН — скважины всех ГЗУ, входящих в его ЦДН;
    - пользователь уровня ГЗУ — только скважины закреплённых ГЗУ."""
    q = db.query(models.Well).join(models.Gzu)
    if user.role == models.ROLE_ADMIN:
        return q
    gzu_ids = [s.object_id for s in user.scope if s.level == "gzu"]
    cdn_ids = [s.object_id for s in user.scope if s.level == "cdn"]
    if user.role == models.ROLE_CDN:
        return q.filter(models.Gzu.cdn_id.in_(cdn_ids or [-1]))
    return q.filter(models.Well.gzu_id.in_(gzu_ids or [-1]))
