from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from .config import Config
from .extensions import db


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    if not app.config["TESTING"] and not app.config["ADMIN_API_KEY"]:
        raise RuntimeError("ADMIN_API_KEY must be configured")
    db.init_app(app)
    from .routes import bp
    from .dashboard import bp as dashboard_bp
    app.register_blueprint(bp)
    app.register_blueprint(dashboard_bp)
    from .playground import bp as playground_bp
    app.register_blueprint(playground_bp)

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        return jsonify(error=error.name.lower().replace(" ", "_"), message=error.description), error.code

    return app
