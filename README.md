# Funded Trading Backend

FastAPI backend for a funded trading app. It uses in-memory dictionaries for now, so data resets when the server restarts.

## Folder Structure

```text
trading-backend/
  main.py
  requirements.txt
  .env.example
  app/
    main.py
    models.py
    storage.py
    services/
      broker_adapter.py
      otp_service.py
      trade_service.py
      user_service.py
```

## Run

```powershell
cd C:\Users\HP\trading-backend
py -m pip install -r requirements.txt
py -m uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000/docs
```

## APIs

### POST `/init-user`

Creates a new challenge user automatically.

### GET `/account/{user_id}`

Returns account details.

### POST `/trade`

Places a virtual trade and deducts balance.

```json
{
  "user_id": "USER_ID",
  "symbol": "NIFTY",
  "side": "BUY",
  "amount": 1000,
  "quantity": 1,
  "entry_price": 23888.3,
  "execute_on_broker": false
}
```

Set `execute_on_broker` to `true` later when real broker API details are configured.

### POST `/close-trade`

Closes a trade and adds the original amount plus profit/loss.

```json
{
  "trade_id": "TRADE_ID",
  "profit_loss": 250
}
```

### POST `/request-activation`

Marks challenge as passed and disables trading until funded activation.

```json
{
  "user_id": "USER_ID"
}
```

### POST `/admin/activate`

Generates a 6-digit OTP. OTP expires in 5 minutes.

```json
{
  "user_id": "USER_ID"
}
```

### POST `/verify-otp`

Activates funded account. OTP is one-time use.

```json
{
  "user_id": "USER_ID",
  "otp": "123456"
}
```

## Broker API Placeholder

Real broker order placement is isolated in:

```text
app/services/broker_adapter.py
```

When you have broker details, fill:

```text
BROKER_BASE_URL=
BROKER_API_KEY=
BROKER_ACCESS_TOKEN=
```

Then replace the TODO section in `place_order()` with the real broker order API call.
