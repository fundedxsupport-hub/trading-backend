from typing import Any, Dict

# Legacy in-memory stores kept only for backward-compatible imports.
# Production code uses MongoDB collections from app.database.
users: Dict[str, Any] = {}
trades: Dict[str, Any] = {}
broker_connections: Dict[str, Any] = {}
