# Weather ETL

A scheduled Python pipeline that ingests hourly weather, turns it into one row per location and UTC day, stores it in SQLite, and writes a three-chart daily report. The scheduler process stays up and runs the job on a cron.

Weather data comes from [Open-Meteo](https://open-meteo.com/).

## Architecture

```
                    config.yaml  +  .env (SMTP and schedule secrets)
                                      |
                                      v
                    +---------------------------------------+
                    |     APScheduler  (python -m etl.scheduler)
                    |     cron from config.yaml, UTC by default
                    |     optional run when the process starts
                    +--------------------+------------------+
                                         | one run
                                         v
          +-------------------+    +-----------------------------+
          | CSV inbox         |    | Open-Meteo forecast API     |
          | data/inbox/*.csv  |    | hourly, past_days + today   |
          +---------+---------+    +-------------+---------------+
                    |   ingest, API retries      |
                    +-------------+--------------+
                                  v
                    +---------------------------------------+
                    | Transform (pandas)                    |
                    |  1. rename column aliases             |
                    |  2. parse timestamps and convert UTC  |
                    |  3. dedupe location + instant         |
                    |  4. aggregate to one row per UTC day  |
                    +--------------------+------------------+
                                         v
                    +---------------------------------------+
                    | Load (SQLAlchemy + SQLite)            |
                    |  daily_weather                        |
                    |  INSERT ... ON CONFLICT DO UPDATE     |
                    |  key: location_name + observation_date|
                    +--------------------+------------------+
                                         v
                    +---------------------------------------+
                    | Report                                |
                    |  temperature, precipitation, wind PNG |
                    |  SMTP email when email.enabled is true|
                    +---------------------------------------+

                    logs/etl.log rotates (RotatingFileHandler)
                    data/heartbeat.txt is the Docker liveness file
```

Processed CSV files move to `data/archive/` after the database commit. A file that cannot be parsed stays in the inbox.

## What one run does

1. Read every `*.csv` in the inbox, then request Open-Meteo for each location. A failed request is retried. Timeout, connection errors, HTTP 429, and HTTP 5xx are retried. Other HTTP 4xx responses are not.
2. Normalize the combined table and upsert the daily rows.
3. Draw the charts from the database, using `report.lookback_days`, so earlier days stay on the chart.
4. Email the charts when email is enabled. The message is still built from the database if this run ingested nothing new.

Retries use exponential backoff: `backoff_seconds * 2^(attempt-1)`, capped at 30 seconds. The default is 3 attempts and a 2 second base, so the waits are 2s and 4s.

If one Open-Meteo location fails and another succeeds, the successful rows are stored and the process exits 1. The scheduler logs that and stays up for the next cron. If every selected source fails, the run raises and nothing is archived.

## Transform rules

Units requested from Open-Meteo are Celsius, millimetres, and km/h. Timestamps are stored as UTC dates.

- Naive timestamps are treated as UTC. Offsets are converted to UTC.
- Rows with a blank location or an unparseable timestamp are dropped.
- The same location and the same instant collapse to one hourly row. The later row wins. A null measurement keeps the earlier value.
- When both CSV and the API are enabled, the API is listed second, so it wins on an overlapping hour.
- Daily precipitation is a sum. Temperature, humidity, and wind are means. A day whose values are all null stays null.
- `observation_count` counts hourly rows, including rows with null metrics.
- `source` lists the distinct hourly sources for that day, sorted, such as `api,csv`.
- Location names are trimmed and stay case-sensitive.

Example: `2026-10-27T20:00:00-04:00` and `2026-10-28T00:00:00Z` are the same instant. They become one hourly row and one daily row for `2026-10-28`.

## Configuration

`config.yaml` is the only settings file. `${VAR}` and `${VAR:-default}` are expanded from the environment. A `.env` file next to the config is loaded on startup. Variables that are already set in the shell win. An empty value uses the default.

| Key | Role |
| --- | --- |
| `schedule` | Cron and timezone for APScheduler. Observations stay UTC even if the cron timezone is local. |
| `schedule.run_on_start` | Run once when the scheduler process starts, then wait for the cron. |
| `ingest.sources` | `csv`, `api`, or both. Order is the override order. |
| `ingest.csv` | Inbox directory, filename glob, and archive directory. The glob is not recursive. |
| `ingest.api` | Open-Meteo URL, timeout, retries, history window, variables, and locations. |
| `database.url` | A `sqlite:///` URL. Relative paths are resolved from the config file's directory. |
| `report` | PNG directory and how many days the charts look back. |
| `email` | SMTP delivery. Leave `enabled: false` until the environment variables below are set. |
| `logging` | Level, rotating file path, size, and backup count. |

Schedule changes apply after a process restart. Location, path, and email edits in `config.yaml` are picked up on the next run.

### Environment variables

| Variable | Used for |
| --- | --- |
| `ETL_CONFIG` | Path to `config.yaml` when `--config` is omitted. |
| `SMTP_HOST` | SMTP server. Required only when email is enabled. |
| `SMTP_PORT` | Default `587`. |
| `SMTP_USER` | SMTP username. Login is skipped when this is empty. |
| `SMTP_PASSWORD` | SMTP password. It is never written to the log or the message. |
| `SMTP_FROM` | From address. |
| `SMTP_TO` | Recipient, or a comma-separated list. |
| `SMTP_USE_TLS` | STARTTLS. Default `true`. |
| `SMTP_USE_SSL` | Implicit TLS, typically port 465. Cannot be combined with STARTTLS. |
| `UID`, `GID` | Host user used by `docker compose` so bind-mounted files stay writable. |
| `ETL_ENV` | `production` by default. `DEBUG` logs are kept only when this is `development`, `dev`, or `test`. |

Copy the template and fill in mail only when you want delivery:

```bash
cp .env.example .env
```

### CSV columns

Required columns are a location and a timestamp. Metric names accept Open-Meteo names and shorter aliases.

| Canonical field | Accepted headers |
| --- | --- |
| location | `location`, `location_name`, `city`, `site`, `station` |
| timestamp | `timestamp`, `time`, `datetime`, `observed_at`, `observation_time` |
| temperature_c | `temperature_c`, `temperature_2m`, `temperature`, `temp_c`, `temp` |
| humidity_pct | `humidity_pct`, `relative_humidity_2m`, `relative_humidity`, `humidity` |
| precipitation_mm | `precipitation_mm`, `precipitation`, `precip_mm`, `precip`, `rain_mm` |
| wind_speed_kmh | `wind_speed_kmh`, `wind_speed_10m`, `wind_speed`, `wind_kmh`, `wind` |
| source | `source` (files without it are tagged `csv`) |

`data/samples/weather_sample.csv` is two cities for 25–27 October 2026. Copy it into the inbox to load it on the next run:

```bash
cp data/samples/weather_sample.csv data/inbox/
```

## Run locally

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m etl --config config.yaml
```

That runs the pipeline once. The scheduler is the long-running process:

```bash
python -m etl.scheduler --config config.yaml
```

The default cron is 06:00 UTC. `run_on_start: true` also runs a job as soon as the scheduler starts. Logs go to `logs/etl.log` and to the console. Charts land in `output/reports/`. The database is `data/weather.db`.

Exit codes for `python -m etl`: `0` success, `1` a run failure or a partial ingest error, `2` a bad config.

## Docker

`docker-compose.yml` defines one service, `scheduler`, which runs `python -m etl.scheduler`. The container reads the mounted `config.yaml` and writes the database, logs, charts, and heartbeat file to bind mounts.

```bash
cp .env.example .env
# set UID and GID to your user so the mounted files stay writable
docker compose up --build -d
docker compose logs -f scheduler
```

One-shot run inside the same image, without starting a second scheduler alongside a live one:

```bash
docker compose run --rm scheduler python -m etl
```

The healthcheck reads `data/heartbeat.txt`. The scheduler writes that file on startup and the pipeline refreshes it on every run. `stop_grace_period` is 30 seconds. An interrupted run can be repeated: the upsert key prevents duplicate days, and CSV files are archived only after the commit.

## Database

`daily_weather` holds one row per location and UTC date.

| Column | Meaning |
| --- | --- |
| `location_name`, `observation_date` | Upsert key |
| `temp_min_c`, `temp_max_c`, `temp_mean_c` | Celsius |
| `precipitation_sum_mm` | Daily sum |
| `humidity_mean_pct`, `wind_speed_mean_kmh` | Daily means |
| `observation_count` | Hourly rows that formed the day |
| `source` | Distinct source labels |
| `loaded_at` | UTC time of the load, stored without a timezone suffix |

SQLite is opened in WAL mode. Tables are created on startup. The database file is local and gitignored. Delete `data/weather.db` to drop the history.

## Tests

```bash
pytest
```

The transform tests cover UTC bucketing, dedupe, aliases, null sums, and empty input. They do not call the network. Additional tests cover the upsert, CSV inbox, config placeholders, chart files, the email body, and one full CSV run against a temporary database.

## Project layout

```
config.yaml
docker-compose.yml
Dockerfile
etl/
  config.py            YAML, paths, environment placeholders
  scheduler.py         APScheduler process
  pipeline.py          one ingest → transform → load → report run
  ingest/              CSV inbox and Open-Meteo client
  transform/weather.py dedupe and daily aggregation
  load/                SQLAlchemy model and SQLite upsert
  report/              matplotlib charts and SMTP
data/inbox/            drop CSVs here
data/archive/          files moved after a successful load
data/samples/          example hourly CSV
output/reports/        PNG charts
logs/etl.log           rotating log, gitignored
scripts/audit_dependencies.sh
tests/                 pytest
```

## Security notes

Secrets stay in the environment, not in git and not in the image.

- Copy `.env.example` to `.env` and put the real SMTP password only in `.env`. `.env` is gitignored. The example file contains placeholders such as `replace-with-a-real-password`.
- `email.smtp_password` in `config.yaml` must be an environment placeholder with an empty default, `${SMTP_PASSWORD:-}`. The loader rejects a literal password or a default secret.
- The password is read from the environment, removed from log lines, and left out of the email. Logs do not include stack traces.
- `ETL_ENV=production` is the default. It forces the log level to INFO. Use `ETL_ENV=development` only when you want DEBUG output on a machine you control.
- The process creates files with umask `027`. The container runs as user `etl` (uid 1000), drops Linux capabilities, and uses a read-only root filesystem. `docker compose` can override the uid so the bind mounts stay writable. `.env`, local databases, and logs are excluded from the image build.
- CSV reads stay inside the inbox, skip symlinks, and reject unsafe file names. Chart files are written only inside the report directory. Database statements use bound parameters.
- This project does not serve HTTP, so browser security headers, CORS, and inbound rate limits are not used. Outbound forecast requests require HTTPS, do not follow redirects, and stop when a response is too large.
- Check dependencies from the project root:

```bash
scripts/audit_dependencies.sh
```

The script installs `pip-audit` into the active environment if it is missing. `python -m pip install safety && safety check -r requirements.txt` is an equivalent check.

## Operational notes

- Keep the cron on a daily cadence. Open-Meteo is a public API and does not need a key for this forecast endpoint.
- Email delivery is off until `email.enabled` is true and the SMTP variables are set. Charts are still written when mail is off.
- A bad CSV stays in `data/inbox/` and is logged on every run until it is fixed or removed. Valid files in the same batch are loaded and archived.
- The scheduler does not exit when a single run fails. `python -m etl` does exit non-zero, which is what you want for a manual or CI run.
- Logging is configured once per process. Changing the log path requires a restart.
