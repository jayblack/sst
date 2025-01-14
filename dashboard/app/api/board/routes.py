from http import HTTPStatus as status

from flask import jsonify, request
from flask_jwt_extended import jwt_required

from app.api.board import bp
from app.extensions import db
from app.models.board import Board
from app.telemetry.psst import dataclass_from_dict


@bp.route('', methods=['GET'])
@jwt_required()
def get_all():
    entities = db.session.execute(db.select(Board)).scalars()
    return jsonify(list(entities)), status.OK


@bp.route('/<string:id>', methods=['DELETE'])
@jwt_required()
def delete(id: str):
    db.session.execute(db.delete(Board).filter_by(id=id))
    db.session.commit()
    return '', status.NO_CONTENT


@bp.route('', methods=['PUT'])
@jwt_required()
def put():
    entity = dataclass_from_dict(Board, request.json)
    if not entity:
        return jsonify(msg="Invalid data for Board"), status.BAD_REQUEST
    entity = db.session.merge(entity)
    db.session.commit()
    return jsonify(id=entity.id), status.CREATED
