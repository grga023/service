# FurnaceDesk

A lightweight, mobile-friendly service desk for gas boiler and stove repair teams. It includes a Kanban board, customer and appliance records, parts tracking, appointment scheduling, role-based accounts, and an Apple/Google-compatible `.ics` calendar export.

## Run with Docker

Docker Engine and the Docker Compose plugin are required. From this directory:

```sh
docker compose up --build -d
docker compose logs -f furnace-desk
```

Open `http://localhost:8081`. Docker maps host port `8081` to port `8000` inside the container to avoid conflicts with other services. To use a different host port, set `PORT` in `.env`. On a first run, the application creates an `admin` account and prints a randomly generated initial password in the container logs. Retrieve it with:

```sh
docker compose logs furnace-desk
```

For a planned installation, create a `.env` file before starting the service:

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-long-unique-password
PORT=8081
COOKIE_SECURE=1
```

Keep `ADMIN_PASSWORD` private. It is used only when the database is empty; changing the environment variable later does not reset the existing account. `COOKIE_SECURE=1` should be used when the site is served over HTTPS. Put the application behind a TLS-enabled reverse proxy for internet-facing deployments; do not expose the built-in HTTP server directly to the public internet.

The SQLite database is stored in the persistent `furnace-data` Docker volume. Back it up regularly. To stop the app without removing its data, run `docker compose down`. Do not remove the volume if you need to keep service records.

## Accounts and roles

The first account is an administrator. Use **Team members** to add accounts and assign any combination of:

- **Administrator** — full access and user management.
- **Call receiver / dispatcher** — creates and manages tickets, schedules visits, and assigns technicians.
- **Service technician** — sees assigned tickets and can update their status, parts, and service notes.

New passwords must be at least 8 characters; use a long, unique password for an internet-facing installation. Deactivating an account prevents it from signing in.

Reset an existing administrator password interactively (the password is not placed in shell history):

```sh
docker compose exec furnace-desk python server.py --reset-admin-password admin
```

Enter the new password twice. Resetting it revokes existing sessions. Administrators can also change passwords from **Team members**.

Login sessions no longer have a fixed application expiry. The browser cookie is renewed for 400 days on authenticated requests (browsers may impose their own storage limits). Closing the browser or restarting the container does not sign you out. Logout, administrator password reset, account deactivation, or clearing browser cookies ends access. This is not a guarantee that a browser will retain a cookie forever.

## Service board and calendar

The active service board shows **Received**, **In progress**, and **Problem**. New services always start in **Received** without asking for a status. Add needed parts as itemized names and quantities; when completing the service, enter the full price charged to the customer and your discounted purchase cost per part. The customer total is parts at the full price plus labor and transport. Earnings are labor plus transport plus the difference between the full parts price and discounted parts cost. The **Done services** tab shows total earnings and each service's earnings breakdown. Older saved part prices are migrated as equal regular and discounted prices until updated, to avoid assuming a profit margin that was not recorded. Optionally estimate the effort using hours (`1h`), eight-hour days (`1d`), or seven-day weeks (`1w`); effort displays in hours (for example, `1.5d` displays as `12h`). Set the appointment start and estimated effort instead of entering a separate appointment end. Drag an active service card to a different column to update its status. Mark an open service done from its service record; it is removed from the active board and calendar, but appears in the **Done services** tab and customer's service history. Enter costs in your preferred currency; the application stores numeric amounts without a currency conversion.

Every saved service creates or updates a customer profile, matched by phone number. Use **Customers** to search by name or phone and review that customer's previous services and appliance models. When creating a new service, selecting a returning customer fills in their contact details automatically. Click a calendar day to create a service already scheduled for that day; click an existing visit or customer-history record to edit it.

Use **Sync calendar** or **Add to device calendar** to download the authenticated `.ics` calendar. The file can be imported into Apple Calendar or Google Calendar on desktop and mobile. The calendar uses local appointment times; importing the file creates a snapshot, so export it again after schedule changes.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8081` | Host port in Docker Compose (mapped to container port `8000`) |
| `ADMIN_USERNAME` | `admin` | Username created on an empty database |
| `ADMIN_PASSWORD` | generated | Initial admin password; set before first start |
| `COOKIE_SECURE` | `0` | Set to `1` when using HTTPS |
| `DATABASE_PATH` | `/data/service.sqlite3` in Docker | SQLite database path |
