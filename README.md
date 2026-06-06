# Niche Finder

Discover your perfect YouTube niche in seconds. Niche Finder analyses real YouTube data to score any topic on demand, competition, and overall opportunity — so you can start a channel with confidence.

**Live demo:** [niche-finder-production-8399.up.railway.app](https://niche-finder-production-8399.up.railway.app)

---

![Niche Finder screenshot](docs/screenshot.png)

> _Replace with an actual screenshot once deployed._

---

## Features

- **Opportunity scoring** — every topic gets a 0–100 score based on real YouTube data
- **Demand score** — how many views top videos are pulling in (log-scale)
- **Competition ease score** — how small the competing channels are (inverted log-scale)
- **Plain-English verdict** — a one-sentence summary of whether the niche is worth pursuing
- **Top video preview** — see the three most relevant videos for any topic
- **Free tier** — 3 searches/day for registered users, 3 total for guests
- **Pro tier** — unlimited daily searches via Stripe subscription (£9.99/month)
- **User accounts** — email/password registration and login with bcrypt-hashed passwords

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask |
| Data | YouTube Data API v3 |
| Auth | Flask-Session, bcrypt, SQLite |
| Payments | Stripe Checkout + Webhooks |
| Hosting | Railway |

## Local setup

### 1. Clone and create a virtual environment

```bash
git clone <your-repo-url>
cd niche-finder
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

| Variable | Where to get it |
|---|---|
| `YOUTUBE_API_KEY` | [Google Cloud Console](https://console.cloud.google.com) → APIs → YouTube Data API v3 |
| `STRIPE_SECRET_KEY` | Stripe Dashboard → Developers → API keys |
| `STRIPE_PUBLISHABLE_KEY` | Stripe Dashboard → Developers → API keys |
| `STRIPE_PRICE_ID` | Create a £9.99/month recurring Price in Stripe Dashboard, copy the `price_...` ID |
| `STRIPE_WEBHOOK_SECRET` | Run `stripe listen --forward-to localhost:8080/webhook`, copy the `whsec_...` |
| `SECRET_KEY` | Any long random string for Flask session signing |

### 3. Run the app

```bash
python app.py
```

Visit [http://localhost:8080](http://localhost:8080).

### 4. Test Stripe webhooks locally

Install the [Stripe CLI](https://stripe.com/docs/stripe-cli) and run:

```bash
stripe listen --forward-to localhost:8080/webhook
```

Use Stripe's test card `4242 4242 4242 4242` (any future expiry, any CVC) to complete a test purchase.

## Project structure

```
niche-finder/
├── app.py               # Flask app — routes, auth, scoring, Stripe
├── requirements.txt
├── .env                 # Local secrets (not committed)
├── niche_finder.db      # SQLite database (auto-created on first run)
└── templates/
    ├── index.html       # Main UI
    ├── login.html
    ├── register.html
    └── success.html     # Post-payment confirmation
```

## Scoring methodology

| Score | Formula |
|---|---|
| Demand | `log10(avg_views + 1) / log10(10,000,001) × 100` |
| Competition ease | `100 − log10(avg_subs + 1) / log10(10,000,001) × 100` |
| Opportunity | `(Demand + Competition ease) / 2` |

Both scores are capped at 0–100. A high opportunity score means a topic has strong viewership relative to the size of channels covering it.
