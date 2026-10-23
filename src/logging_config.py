import json
import logging
import os


def configure_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(message)s")

    class JsonFormatter(logging.Formatter):
        def format(self, record):
            payload = {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            return json.dumps(payload)

    root = logging.getLogger()
    for h in root.handlers:
        h.setFormatter(JsonFormatter())