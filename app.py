from flask import Flask, jsonify

from config import load_settings, Settings


def create_app() -> Flask:
    app = Flask(__name__)

    # Load config once at startup
    settings: Settings = load_settings()
    app.config["SETTINGS"] = settings

    @app.route("/", methods=["GET"])
    def index():
        return "Email → Telegram reminder bot is running", 200

    @app.route("/health", methods=["GET"])
    def health():
        # Don't expose secrets, just a minimal sanity check
        return jsonify(
            {
                "status": "ok",
                "timezone": settings.timezone,
            }
        ), 200

    return app


app = create_app()

if __name__ == "__main__":
    # For local development only. On Render we'll use gunicorn.
    app.run(host="0.0.0.0", port=5001, debug=True)