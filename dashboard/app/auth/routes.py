from argon2 import PasswordHasher
from http import HTTPStatus as status

from flask import jsonify, request
from flask_jwt_extended import (
    create_access_token,
    current_user,
    jwt_required,
    set_access_cookies,
    unset_jwt_cookies
)
from app.auth import bp
from app.extensions import db
from app.models.user import User


@bp.route("/login", methods=["POST"])
def login():
    username = request.json.get('username', None)
    password = request.json.get('password', None)

    user = db.session.execute(
        db.select(User).filter_by(username=username)).scalar_one_or_none()
    if not user or not user.check_password(password):
        return jsonify(msg="Wrong username or password"), status.FORBIDDEN

    access_token = create_access_token(identity=user)
    response = jsonify({"access_token": access_token})
    set_access_cookies(response, access_token)
    return response


@bp.route("/pwchange", methods=["PATCH"])
@jwt_required()
def password_change():
    old_password = request.json.get('old_password', None)
    new_password = request.json.get('new_password', None)
    if not current_user.check_password(old_password):
        return jsonify(msg="Wrong password"), status.FORBIDDEN
    if len(new_password) < 10:
        return jsonify(msg="Password is not secure"), status.BAD_REQUEST

    ph = PasswordHasher()
    current_user.hash = ph.hash(new_password)
    db.session.merge(current_user)
    db.session.commit()

    return '', status.NO_CONTENT


@bp.route("/logout", methods=["POST"])
def logout():
    response = jsonify({"msg": "logout successful"})
    unset_jwt_cookies(response)
    return response


@bp.route("/user", methods=["GET"])
@jwt_required()
def get_user():
    return jsonify(
        id=current_user.id,
        username=current_user.username,
    )
