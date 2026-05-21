# eCommerce SRE Assignment Starter

This is the starter scaffold for a junior SRE take-home assignment. It is a
working but **deliberately uninstrumented** eCommerce application. There are no
metrics endpoints, no structured logs, no dashboards, and no AI tooling. Adding
all of that — and building a system that lets an LLM reason about the running
stack — is the assignment.

## Stack

- Node 20 + Express + TypeScript (backend)
- SQLite via better-sqlite3 (embedded, zero setup)
- JWT auth with bcrypt password hashing
- React 18 + Vite + TypeScript (frontend)
- Docker Compose

## Run it

```bash
docker compose up --build
```

Frontend: http://localhost:5173  
Backend: http://localhost:4000  
Demo credentials: `demo@shop.local` / `demopass`

**Local dev (no Docker):**

```bash
# terminal 1
cd backend && npm install && npm run seed && npm run dev

# terminal 2
cd frontend && npm install && npm run dev
```

## API surface

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | /api/auth/signup | — | Create account, returns JWT |
| POST | /api/auth/login | — | Login, returns JWT |
| GET | /api/products | — | List products (supports `?search=` and `?category=`) |
| GET | /api/products/:id | — | Get single product |
| GET | /api/products/:id/related | — | "Customers also bought" (self-join, deliberately slow) |
| GET | /api/cart | JWT | Get current user's cart |
| POST | /api/cart/items | JWT | Add item to cart |
| DELETE | /api/cart/items/:productId | JWT | Remove item from cart |
| POST | /api/checkout | JWT | Convert cart to order, returns `order_id` |
| GET | /api/checkout/:orderId | JWT | Get order details and items |
| POST | /api/payment | JWT | Pay an order (mock provider) |
| GET | /healthz | — | Health check |

## User journey

Browse catalog → add to cart → checkout (creates an order, decrements stock)
→ payment (calls mock provider, 120–450ms latency, ~8% failure rate) → success
page.

The payment failure rate is controlled by the `PAYMENT_FAILURE_RATE` env var
(default `0.08`). Set it to `0.5` to make failures common enough to show up on
a dashboard immediately.

## Intentional behaviors worth observing

These are not bugs. They are signals. Instrument them.

- `GET /api/products/:id/related` runs an un-indexed self-join across
  `order_items`. Latency grows with order volume — a textbook slow query to
  catch with a histogram and a slow-query log.

- Payment latency is uniformly random in `[120ms, 450ms]`. A p95 histogram
  panel will show this clearly; a p50 panel won't — that difference matters.

- `PAYMENT_FAILURE_RATE` is hot-configurable. Turning it up and watching your
  error-rate panel respond is a fast way to prove your instrumentation works.

- Auth failures return `401`. Checkout and payment have distinct error codes:
  `empty_cart`, `insufficient_stock`, `order_not_payable`, `payment_declined`.
  Each is a separate log facet worth counting.

- Product search uses `LIKE '%...%'` with no full-text index. On 120 rows it's
  fast; with real volume it degrades predictably.

## What's intentionally missing

No `/metrics` endpoint. No structured log format. No log shipper. No Grafana
dashboards. No Prometheus. No Elasticsearch. No tracing. No AI tooling.

That's the assignment.

## Project layout

```
.
├── docker-compose.yml
├── backend/
│   ├── Dockerfile
│   ├── package.json
│   ├── tsconfig.json
│   └── src/
│       ├── index.ts          Express app entry point
│       ├── config.ts         Env-driven config
│       ├── db.ts             SQLite init + schema
│       ├── auth.ts           JWT sign + requireAuth middleware
│       ├── util.ts           asyncHandler, HttpError, sleep
│       ├── seed.ts           120 products + demo user
│       └── routes/
│           ├── auth.ts
│           ├── products.ts
│           ├── cart.ts
│           ├── checkout.ts
│           └── payment.ts
└── frontend/
    ├── Dockerfile
    ├── nginx.conf
    ├── package.json
    ├── tsconfig.json
    ├── vite.config.ts
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── api.ts            Typed fetch wrapper
        ├── styles.css
        └── pages/
            ├── Login.tsx
            ├── Products.tsx
            ├── Cart.tsx
            ├── Checkout.tsx
            ├── Payment.tsx
            └── OrderSuccess.tsx
```
