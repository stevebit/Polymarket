# Local PostgreSQL — mirror Azure `weather` data (Windows)

Use this when you want **pgAdmin**, **DBeaver**, **psql**, or the Python app pointed at a **local** copy of the same schema and rows as Azure.

## 1. Install PostgreSQL on Windows

1. Download the installer from [PostgreSQL Windows downloads](https://www.postgresql.org/download/windows/) (EDB installer) or use `winget install PostgreSQL.PostgreSQL` and note the version.
2. During setup, remember the **password** for the `postgres` superuser.
3. Leave port **5432** unless you already use it.
4. Add the `bin` folder to your PATH (optional but convenient), for example:

   `C:\Program Files\PostgreSQL\18\bin`

   (Adjust the number if you installed a different major version.)

5. Confirm in **PowerShell**:

   ```powershell
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" --version
   ```

## 2. Create an empty local database

Pick a name that will not clash with anything else, e.g. `weather_local`.

```powershell
$PG = "C:\Program Files\PostgreSQL\18\bin"
& "$PG\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -d postgres -c "CREATE DATABASE weather_local;"
```

If `CREATE DATABASE` says it already exists and you want a fresh copy:

```powershell
& "$PG\psql.exe" -U postgres -h 127.0.0.1 -d postgres -c "DROP DATABASE IF EXISTS weather_local WITH (FORCE);"
& "$PG\psql.exe" -U postgres -h 127.0.0.1 -d postgres -c "CREATE DATABASE weather_local;"
```

## 3. Allow your PC to reach Azure (one-time per IP)

Azure Postgres uses a **firewall**. From the machine that will run `pg_dump`, your public IP must be allowed (same as when we fixed Cursor’s IP).

```powershell
# Your public IP
(Invoke-WebRequest -Uri "https://api.ipify.org" -UseBasicParsing).Content

# Then in Azure Portal: Flexible server → Networking → add firewall rule,
# or Azure CLI (replace IP and rule name):
az postgres flexible-server firewall-rule create `
  --resource-group polymarket-weather-rg `
  --name polymarket-weather-pg-5ba2b0 `
  --rule-name allow-my-home-pc `
  --start-ip-address YOUR.IP.HERE `
  --end-ip-address YOUR.IP.HERE
```

## 4. Copy Azure → local (logical dump)

Your repo `.env` should already define **`WEATHER_POSTGRES_URL`** (Azure libpq URL with `sslmode=require`). **Do not commit it or paste it into chat.**

### Option A — run the helper script (recommended)

From the repo root, set the Azure URL in the environment for this session only, then run:

```powershell
cd C:\Users\steve\Git_Code\Polymarket

# Load Azure URL from .env without printing it (dot-sourcing a tiny setter)
# Easiest: copy WEATHER_POSTGRES_URL from .env and run in same shell:
#   $env:WEATHER_POSTGRES_URL = "postgresql://..."

.\scripts\sync_azure_weather_to_local.ps1 `
  -PostgresBin "C:\Program Files\PostgreSQL\18\bin" `
  -LocalUrl "postgresql://postgres:YOUR_LOCAL_PASSWORD@127.0.0.1:5432/weather_local?sslmode=disable" `
  -DumpPath ".\data\azure_weather.dump"
```

The script runs `pg_dump` from Azure, then `pg_restore` into the local DB. The `data\` folder is gitignored — good place for the dump file.

### Option B — manual commands

```powershell
$PG = "C:\Program Files\PostgreSQL\18\bin"
$env:PGPASSWORD = "<azure-password>"   # OR embed in URI; unset after
& "$PG\pg_dump.exe" "<paste WEATHER_POSTGRES_URL here>" `
  -Fc -f .\data\azure_weather.dump --no-owner --no-acl -v

$env:PGPASSWORD = "<local-postgres-password>"
& "$PG\pg_restore.exe" `
  --dbname "postgresql://postgres@127.0.0.1:5432/weather_local?sslmode=disable" `
  --verbose --clean --if-exists --no-owner `
  .\data\azure_weather.dump
```

Notes:

- **Custom format** (`-Fc`) + `pg_restore` is the usual pair on Windows.
- `--no-owner --no-acl` avoids permission mismatches between `weatheradmin` on Azure and `postgres` locally.
- If restore errors on extensions Azure has that you did not install locally, install matching extensions in local Postgres (often `pgcrypto` for `gen_random_uuid()` — your migrations use it).

## 5. Extensions (usually nothing to do)

PostgreSQL **13+** includes `gen_random_uuid()` without enabling `pgcrypto`. If
`pg_restore` ever complains about a missing extension, install it on the local
server (e.g. `CREATE EXTENSION IF NOT EXISTS pgcrypto;`) and re-run restore
with `--clean`.

## 6. Point the Python app at local Postgres (optional)

For exploration you can **temporarily** set in `.env`:

```env
WEATHER_POSTGRES_URL=postgresql://postgres:YOUR_LOCAL_PASSWORD@127.0.0.1:5432/weather_local?sslmode=disable
```

Then:

```powershell
.\.venv\Scripts\python.exe -m polymarket_weather.cli.migrate   # should no-op if already applied
.\.venv\Scripts\python.exe -c "from polymarket_weather.db import with_conn; 
with with_conn() as c, c.cursor() as cur: 
  cur.execute('SELECT count(*) FROM stations'); print(cur.fetchone())"
```

Switch the URL back to Azure when you want cloud data again.

## 7. Browse data visually

- **pgAdmin**: add server `localhost`, database `weather_local`, user `postgres`.
- **DBeaver**: new PostgreSQL connection with the same settings.
- Useful tables: `stations`, `pm_events`, `pm_buckets`, `pm_market_snapshots`, `forecasts`, `observations`, `hourly_observations`, `predictions`, `bucket_probs`, `paper_trades`, `orders`, `fills`, `daily_pnl`, `schema_migrations`.

## 8. Refresh the copy later

Re-run the same `pg_dump` + `pg_restore --clean` flow (or the script). `--clean` drops objects before recreate; your local DB is disposable relative to Azure.

---

**Security:** never commit `azure_weather.dump` or `.env` with passwords. Keep dumps under `data/` (gitignored) or outside the repo.
