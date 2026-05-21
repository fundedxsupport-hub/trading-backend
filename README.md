# FundedX Trading Backend

FastAPI + MongoDB backend for Challenge Account, Real Account, admin APIs, wallet control, support tickets, MPIN, client IDs, referral history, risk control, and Upstox master-account trade execution.

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env
uvicorn main:app --host 0.0.0.0 --port 9000 --reload
```

MongoDB must be running and `MONGO_URL` must point to it.

## Main APIs

- `POST /register` - create user, unique email/mobile/client ID `FIX123456`, referral code.
- `GET /account/{user_id}` - account summary for Challenge and Real account top sections.
- `GET /wallet/{account_type}/{user_id}` - wallet values. Challenge returns virtual capital, profit, loss, balance.
- `POST /admin/wallet/challenge` - admin sets Challenge virtual capital.
- `POST /admin/wallet/real` - admin sets Real capital.
- `POST /plan/active` - set/replace active plan; only one active plan is stored.
- `POST /support/tickets` - client support ticket with unique complaint ID.
- `GET /admin/support` - admin support list with user name, user ID, complaint ID, message, timestamp.
- `POST /admin/support/{complaint_id}/reply` - admin reply to client issue.
- `POST /mpin/request-otp`, `POST /mpin/change`, `POST /mpin/login` - MPIN security flow.
- `GET /admin/referrals` - referral tracking history with payment status.
- `POST /trade` - Challenge or Real trade placement; Real trades can execute through Upstox.
- `POST /close-trade` - closes trade and updates wallet P/L only after close.
- `GET /admin/trades?account_type=real` - admin trade status.

## Upstox

Keep `DEMO_BROKER_MODE=true` for safe demo orders. For live execution set:

```env
DEMO_BROKER_MODE=false
UPSTOX_ACCESS_TOKEN=your_access_token
UPSTOX_BASE_URL=https://api.upstox.com/v2
```

Never commit real tokens in `.env.example`.

## Secrets

Store private values only in:

`C:\Users\HP\trading-backend\.env`

Recommended keys:

- `MONGO_URL`
- `MONGO_DB_NAME`
- `UPSTOX_ACCESS_TOKEN`
- `UPSTOX_API_KEY`
- `UPSTOX_API_SECRET`
- `DEMO_BROKER_MODE=false` for live mode

Do not place Upstox secrets inside `fundedx-app` or `fundedx-admin`.
