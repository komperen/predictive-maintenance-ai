# Predictive Maintenance AI

Manufacturing predictive-maintenance demo built with **H2O Wave**, designed to run on **HPE Private Cloud AI (PCAI)** with an on-prem Qwen model served by MLIS.

## Features

- **Overview** - live sensor sparklines per machine, fleet status, plant OEE
- **Production & OEE** - OEE (availability x performance x quality), output vs plan, downtime cost, energy per machine, auto-generated **shift handover** (optional AI rewrite)
- **Alerts & Work Orders** - plain-language alerts, automatic work orders, click-to-assign **engineer on duty**, duty roster
- **Work-order knowledge base** - safety / LOTO, likely causes, step-by-step SOP, spare parts for the repair, **AI diagnosis**
- **Predictive Health** - health score, trend-based time-to-threshold, downtime avoided (Rp)
- **Parts & PM Plan** - preventive-maintenance schedule (click to mark done), spare-parts inventory (order / receive), readiness of parts for each at-risk machine; completing a work order consumes parts
- **AI Copilot** - ask the fleet in English or Bahasa; PCAI Qwen, OpenRouter free models, or built-in simulation
- **Telegram notifications** for new alerts, assignments and predictions

Everything runs without any AI key (Simulation mode). Add the MLIS token to switch to the PCAI model.

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `LLM_BASE_URL` | MLIS / OpenAI-compatible endpoint (`/v1` is added if missing) | PCAI Qwen endpoint |
| `LLM_MODEL` | Model name | `qwen3.8-27b` |
| `LLM_API_KEY` | **Secret.** MLIS access token. When set, the AI provider starts as PCAI | empty (Simulation) |
| `LLM_VERIFY_SSL` | `false` for the lab's self-signed certificate | `true` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | **Secret.** Optional notifications | empty |
| `OPENROUTER_API_KEY` | **Secret.** Optional OpenRouter free models | empty |

> Never commit tokens. `.env` is git-ignored. In Kubernetes use a Secret (see below) or paste the token in the app's **Settings** page.

## Run locally

```bash
cp .env.example .env        # fill in LLM_API_KEY if you want the AI model
docker compose up --build   # open http://localhost:8080
```

or without Docker: `pip install -r requirements.txt && wave run app` (needs the Wave server).

## Deploy to HPE PCAI

1. Push this repo to GitHub - the workflow `.github/workflows/build.yml` tests the app, builds the image and pushes `ghcr.io/<owner>/predictive-maintenance-ai:<version>`; it also lints and packages the Helm chart.
2. Make the GHCR package **Public** (or create an `imagePullSecret`).
3. In PCAI: **Tools & Frameworks -> Import Framework**, upload `predictive-maintenance-ai-0.1.0.tgz` (from `deploy/helm`, built by CI, or packaged with `helm package deploy/helm/predictive-maintenance-ai`). Check `image.repository` in the values.
4. Provide the token, preferably as a Secret:

   ```bash
   kubectl -n <app-namespace> create secret generic pm-ai-secrets --from-literal=LLM_API_KEY='<MLIS token>'
   ```

   then set `llm.existingSecret: pm-ai-secrets` in the chart values (or leave it empty and paste the token in the app's Settings page).
5. Open `https://pm-ai.<your-domain>`.

If the pod cannot resolve `*.imid.local`, add `hostAliases` in the values.

## Run the tests

```bash
pip install h2o-wave requests
python tests/test_offline_harness.py
```

## H2O AI Cloud

`app.toml` is included, so the same code can be uploaded as a zip (My Apps -> Upload App). Bump `Version` for every upload.

## Demo data

Machines, engineers, fault knowledge base, spare parts and PM plan are plain JSON in `sample_data/`. Downtime cost, energy tariff and OEE target are demo assumptions at the top of `app.py`.
