# Garmin Morning

A personal **LangGraph + Gemini** agent that turns Garmin data into a daily morning email: yesterday's biomarkers and activities, the sleep ending this morning, and practical movement and meal suggestions for today.

The dashboard runs locally on your Mac. It includes preferences, a saved food library, connection tests, report previews and history, and daily scheduling at **10 AM in your selected timezone**.

## Get started

Python 3.11 or newer is required. The project's existing `.venv` can be used:

```sh
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/garmin-agent --data-dir .data serve
```

Open **http://127.0.0.1:8765**. Using `--data-dir .data` keeps local state inside this project. Without this option, the app uses `~/Library/Application Support/GarminMorning`. Use the **same data directory on every command**.

1. **Preferences:** set your email, timezone, units, goals, preferred activities, available time, equipment, limitations, diet/allergen tags, sleep goal and optional hydration target.
2. **Connections:** save the Garmin MCP executable/session directory, Gemini API key and model, and SMTP settings. The Gemini model is editable because API access and available models vary; use an ID available to your key. The default follows the Flash example in the [LangChain integration documentation](https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai).
3. **Food library:** register your usual foods or meals, a serving description, calories per serving, ingredients, declared allergens and diet tags. A food must match **every required diet tag** and must not share any declared allergen tag with your exclusions. Use consistent tags, for example `dairy`; the app does not infer allergen aliases from ingredient text.
4. **Test all three connections**, then create a preview. Preview generation can use Garmin and Gemini, but never sends email.
5. **Enable daily schedule** on Overview. This checks all connections and starts with the next upcoming 10 AM. The explicit **Generate & email today** button can send today's report immediately.

The app never estimates a daily calorie requirement. It calculates suggested meal calories from your saved servings, including portion multipliers. These are suggested meals, not logged food intake. An empty or unsuitable food catalog produces an explanation instead of invented foods.

## Connect Garmin

This app communicates through the existing Garmin MCP's STDIO interface. It does not call Garmin Connect directly to retrieve health data. The default executable is:

```text
~/PycharmProjects/garmin_mcp/.venv/bin/python
```

Arguments are the JSON array `["-m", "garmin_mcp"]`. Change the full executable path for another installation. The subprocess receives `GARTH_SESSION_PATH` explicitly, and no Gemini or SMTP credentials. Required tools are discovered and checked when connecting.

Reuse an existing session at `~/.garth`, or create one with an interactive terminal login:

```sh
.venv/bin/garmin-agent --data-dir .data auth
```

This uses garth only for the authentication bootstrap, supports its terminal MFA prompt, and saves the session in the directory configured in Connections. It does not store your Garmin password. If the MCP project has not been installed, install it using its own setup instructions first. No changes to that neighboring project are required.

## Gemini and email

API and SMTP passwords entered in the dashboard are stored in **macOS Keychain**. Non-secret configuration, foods, normalized reports and graph checkpoints are stored in SQLite under the data directory, which is restricted to your OS user. Keychain may ask you to allow access by the Python interpreter.

For explicit deployment overrides you may supply `GEMINI_API_KEY` and `SMTP_PASSWORD` in the process environment; these take precedence over Keychain. Avoid putting keys in command arguments or committing them to files. A LaunchAgent does not inherit your terminal environment, so Keychain is the recommended setup.

Gemini receives normalized health observations, calculated trends, your fitness/diet preferences and eligible food entries. Account IDs, recipient address, activity names, routes and credentials are excluded. External LangSmith tracing is disabled by the CLI and installed LaunchAgent.

SMTP supports verified TLS only:

- **STARTTLS**, usually port **587**.
- **Implicit TLS**, usually port **465**.

For Gmail, use `smtp.gmail.com`, your full email as the username, and a Google app password where supported. Other authenticated SMTP providers work too. The recipient is set in Preferences; sender defaults to the SMTP username.

## Run every morning on macOS

After checking your preview, stop the terminal server with Ctrl-C and install the background service, using the same data directory:

```sh
.venv/bin/garmin-agent --data-dir .data schedule install
.venv/bin/garmin-agent --data-dir .data schedule status
```

The installer creates `~/Library/LaunchAgents/com.garminmorning.agent.plist` and starts the app at login with absolute interpreter and data paths. It needs no root access. Keep the project and virtual environment at their current paths, or reinstall the LaunchAgent after moving them. Dashboard schedule enable/disable is independent of service installation.

The service checks whether a report is due every 30 seconds. Generation starts at or shortly after **10 AM**, and email follows collection and analysis. Your Mac must be awake, connected and logged in. When it resumes later the same day, the app catches up once and marks the report delayed. On subsequent days it sends only the current day's report, not a backlog. It does not wake a sleeping Mac. Timezone selection is independent of your Mac's system timezone and handles daylight saving.

To remove the background service while keeping settings and history:

```sh
.venv/bin/garmin-agent --data-dir .data schedule uninstall
```

Service logs are `service.log` and `service-error.log` in the data directory. The dashboard shows connection tests and run errors. Changing email recipient, MCP, Gemini model or SMTP configuration pauses scheduling until you enable it again. Saving a new secret does the same.

## Preview from the terminal

```sh
.venv/bin/garmin-agent --data-dir .data run --dry-run --output /tmp/garmin-preview.html
```

Omit `--dry-run` to send today's report. The dashboard can display HTML and plain text (`/reports/{id}?format=text`). A successfully delivered date is never automatically sent again, even if you click Run again. Preview runs are separate and may be repeated.

## Workflow and reliability

```text
load preferences → collect via MCP → normalize and calculate
  → Gemini analysis / unavailable-data branch → validate
  → render HTML + plain text → preview / SMTP delivery
```

- Reports distinguish yesterday's wellness and activities from sleep/HRV dated today. Sleep timestamps are checked so an older night cannot be mislabeled as last night.
- Daily metrics are compared to the preceding seven days where at least three valid observations exist. Missing and negative sentinel readings are unavailable, never fabricated as zeros.
- Activities are paginated and selected using UTC timestamps converted to your reporting timezone where available. The fallback uses Garmin's recorded local date. Pagination is bounded, with a visible warning if incomplete.
- Transient Garmin and Gemini failures receive up to three attempts. Invalid Gemini output is corrected within that budget, then falls back to a factual report. Missing optional Garmin data produces visible data notes.
- Food IDs, eligible meal types, portion limits, preferred activities, exercise time and minimum recovery-data constraints are checked before rendering. Calories and statistics are calculated in Python.
- SQLite checkpoints allow interrupted workflows to resume. Report content snapshots preserve preferences, foods and serving values used at generation time. A process lock prevents overlapping runs.
- A successful SMTP submission is recorded as **sent** (this indicates server acceptance, not guaranteed inbox placement). Explicit rejection is **failed**; the scheduler retries failed runs after a 30-minute cooldown. An interruption or lost response during submission becomes **uncertain**, and that date will not be resent automatically. Check your inbox and SMTP provider manually. Next day's report proceeds normally.
- Model failures produce factual summaries. If no useful Garmin data is available, the report explains the missing connection/sync data and omits personalized exercise and meal plans.

The localhost dashboard uses same-origin CSRF protection, trusted-host validation and escaped content. It is for one OS user and is not designed for public hosting. Health data and report history remain on disk until you remove the data directory; there is no automatic retention purge. Suggestions are general wellness guidance, not diagnoses or treatment.

## Development and tests

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
node --check src/garmin_agent/static/app.js
```

The test suite needs no Garmin, Gemini or SMTP credentials. It includes a real STDIO MCP subprocess fixture, mocked model/SMTP failures, graph recovery, concurrency, timezones/DST, calorie arithmetic, food constraints, HTML escaping and local API protections.

`requirements.lock` records the verified environment, including development tools. For that exact environment, install it before the editable package:

```sh
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
```

HTMX 2.0.8 is bundled locally in `static/htmx.min.js` so dashboard updates work without a CDN. It is distributed under the [Zero-Clause BSD license](https://github.com/bigskysoftware/htmx/blob/v2.0.8/LICENSE).
