from app.models import Trade, UserAccount


# In-memory stores. Replace these with database repositories later.
users: dict[str, UserAccount] = {}
trades: dict[str, Trade] = {}
