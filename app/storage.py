from app.models import BrokerConnection, Trade, UserAccount


# In-memory stores. Replace these with database repositories later.
users: dict[str, UserAccount] = {}
trades: dict[str, Trade] = {}
broker_connections: dict[str, BrokerConnection] = {}
