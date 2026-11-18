# Square setup (Step 2)

The tracker reads Square; it never changes anything there. Until the settings below are in
a server's `.env`, Square is off on that server and the tracker works as in Step 1.

Do staging first, with the Square **sandbox**. Production uses the real account, later.

## A. The Square application (developer.squareup.com, signed in to Graphnix's Square account)

The application must belong to **Graphnix's own Square account**, the one the shop sells
through, signed in as its owner (Jenn or Nick) or as a team member they've given developer
access. An app made under a personal Square account has its own separate sandbox and
production: its production token can't see Graphnix's customers, invoices or orders. One app
serves both stages: the sandbox side for staging, the production side for production.

1. Sign in at developer.squareup.com with Graphnix's Square login. **Applications → +** and
   name it `GRAPHNIX tracker`.
2. Switch to **Sandbox** at the top of the page.
3. **Credentials**: copy the **Sandbox access token**. It is a secret.
4. **Locations**: copy the sandbox **Location ID**.
5. **Webhooks → Subscriptions → Add subscription**:
   - Name: `Tracker staging`
   - URL: `https://staging.jobs.graphnix.com/integrations/square/webhook/`
   - API version: the newest offered
   - Events: `invoice.created`, `invoice.published`, `invoice.updated`,
     `invoice.payment_made`, `invoice.refunded`, `invoice.canceled`, `invoice.deleted`,
     `invoice.scheduled_charge_failed`, `customer.created`, `customer.updated`,
     `order.created`, `order.updated`
   - Save, then copy the subscription's **Signature key**. It is a secret.

## B. The staging server's `.env`

Add these, then redeploy (or restart `web` and `worker`):

```
SQUARE_ENVIRONMENT=sandbox
SQUARE_ACCESS_TOKEN=<sandbox access token>
SQUARE_WEBHOOK_SIGNATURE_KEY=<subscription signature key>
SQUARE_WEBHOOK_URL=https://staging.jobs.graphnix.com/integrations/square/webhook/
SQUARE_LOCATION_ID=<sandbox location id>
```

`SQUARE_WEBHOOK_URL` must match the subscription URL character for character, slash
included: Square signs it with every delivery.

## C. Test it

1. In the tracker, note a job's **G-** number (on the job page and in search).
2. In the Square sandbox, create an invoice with that number in its **title**, for example
   `G-1042 team tees`, and send it. Keep the dash: `G1042` isn't read as a job number, so a
   blank's style code such as Gildan `G2000` never links an invoice by mistake.
3. Within a minute the job is at **Invoiced** (if it was still at a quote stage), and its
   Details tab shows the payment status. The timeline shows "Square invoice … linked".
4. Pay it in the sandbox (the invoice page has a test card). The job shows **Paid**.

Most invoices need nothing from staff: an invoice from an accepted estimate links to the
estimate's job by itself (see **Estimates** below). For an invoice made directly in Square,
**put the job's number, with its dash, in the invoice title** (`G-1042`). An invoice the
tracker can't place waits under **Invoices** in the header (Admin and Manager): type the
job number and **Link**, **Create job** to start a job from it, or **Not a job** for a sale
with no production. A wrong match is fixed with **Unlink** in the job's Payment panel
(Details tab).

The token, the location and the invoices must all belong to the same Square account. In
the sandbox, each test account has its own; use the **Default Test Account** for all three.

## D. What runs by itself

- **Payment gates.** By default a job needs **payment in full before Materials ordered**.
  The button explains why it's blocked; an Admin can still move it, with a reason, and
  that's in the audit log. Change the gates at **Home › Invoices › Payment gates**. Gates
  only apply on a server where Square is set up.
- **Nightly check, 03:00.** Re-reads invoices on open jobs and any updated in the last 3
  days, retries events that failed, and deletes webhook bodies older than 90 days.
- **Health panel** (`/ops/health/`): last webhook, events stuck or skipped (with the
  reason, such as a token that can't read the location), and the last nightly check.

## E. Customers at go-live (once, per server)

Creates or links tracker customers for everyone invoiced in Square in the last 2 years.
It only reads Square. Try it with `--dry-run` first:

```bash
docker compose exec web python manage.py import_square_customers --dry-run
docker compose exec web python manage.py import_square_customers
```

It reads every invoice first, then checks each customer: a thousand customers take a few
minutes, and it prints its progress as it goes. Customers already linked are skipped, so
it's safe to run again (or to stop with Ctrl+C and rerun). After go-live, a new Square
customer whose email matches a tracker customer links itself.

## Online store orders

Orders from the Square Online stores skip the quote and invoice: they're paid at checkout and
use the store's approved design. Each one becomes a job at **Mock-Up Approved**:

- the buyer is the customer (linked by their Square customer, else matched by email, else
  created);
- the order's lines become job items, with sizes read from the variation names ("Black /
  Large" → colour Black, size L) and personalization kept in the item notes;
- the due date runs from the order, as for any mock-up approval;
- payment comes from the order: Paid, Refunded or Cancelled.

Counter sales are Square orders too, and are ignored: only orders whose **source** contains
a name in `SQUARE_STORE_SOURCES` (default `Square Online`) become jobs. Check the real source
names before go-live:

```bash
docker compose exec web python manage.py import_square_orders --live --location LXXXX --list
```

It lists the last 30 days of orders and counts them by source, marking the ones that would
become jobs. If the stores show under another name, set it in `.env`, comma-separated:
`SQUARE_STORE_SOURCES=Square Online,Team Shop`.

Each job takes the store order's number as its job number (shown instead of a G- number).
It carries a **New Online Order** badge on the board, Home, search and the job page while it
waits at Mock-Up Approved; once someone moves it on, the badge reads **Online Order**.

The webhook subscription needs `order.created` and `order.updated` (section A). The nightly
check also reads store orders updated in the last 3 days, so a missed webhook still makes
its job.

**At go-live,** bring in the store orders already placed but not yet made: every order
still open (not completed or cancelled) from the last year. Preview first:

```bash
docker compose exec web python manage.py import_square_orders --open --dry-run
docker compose exec web python manage.py import_square_orders --open
```

`--days 90` looks back less far. Orders already in the tracker are skipped, so it's safe to
run again.

## Estimates (from Square's emails)

Square's API has no estimates. Square does email the business when an estimate is **sent**
("A new estimate was sent to Pat Doe (#000685)") and when the customer **accepts** it ("You
accepted an estimate! (#000685)"). The tracker reads those two kinds of email from
`info@graphnix.com` every 5 minutes. It opens only mail from Square's two sending
addresses, and keeps only what it reads from them, never the email itself.

- **Sent:** the job takes the estimate number as its number (shown instead of the G-
  number) and moves to **Quote Sent**. The job is, in order:
  1. the one already carrying that estimate number;
  2. the one job waiting at **Quoting** (with no estimate yet) for that customer, found by
     email (the customer's, one of their contacts', or the website request's the job came
     from), else by name;
  3. else a new job named for the customer ("000685 · Pat Doe"), and a new customer if
     the email is new.

  If two Quoting jobs fit, the estimate waits under **Invoices › Estimates** to be linked,
  made into a job, or set aside.
- **Accepted:** the job moves to **Invoiced** (there's no separate Quote Approved stage:
  Square turns an accepted estimate into an invoice at once). That invoice links to the
  job by customer and total and brings its payment status.
- **Follow up:** quotes sent 7 or more days ago with no answer are listed on Home, in the
  Quotes panel. Change the 7 with the app setting `quote_follow_up_days` (Admin › App
  settings).

### Setting it up

1. **Get Square's emails into info@.** They go to Square's account owners (Jenn and Nick).
   In one of their Outlooks, add a rule: from `invoicing@messaging.squareup.com` or
   `messenger@messaging.squareup.com`, with `estimate` in the subject, **Redirect to**
   `info@graphnix.com`. Use *Redirect*, not *Forward*: a forwarded email shows the owner as
   the sender, and the tracker won't recognise it.
2. **Let the tracker read info@** (once per tenant). In Exchange Online PowerShell, add
   the read role to the scope that already limits sending (server setup, section 9):
   ```powershell
   New-ManagementRoleAssignment -App <client id> -Role "Application Mail.Read" -CustomResourceScope "Graphnix tracker mailboxes"
   Test-ServicePrincipalAuthorization -Identity <client id> -Resource info@graphnix.com
   ```
   Don't add `Mail.Read` under API permissions in Entra: that grant covers every mailbox.
3. **Each server's `.env`:** `ESTIMATE_MAILBOX=info@graphnix.com`, then restart `web` and
   `worker`. Only one server should read estimates for real: staging and production would
   otherwise both act on the same emails. Leave it off on staging once production is live.
4. **Try it:** send an estimate to yourself, then run (or wait 5 minutes):
   ```bash
   docker compose exec web python manage.py check_estimate_mail
   ```
   The job appears at Quote Sent with the estimate number. `--days 30` reads further back
   (each email is handled once, so it's safe to repeat). The health panel shows the last
   read and any error.

   To see first what a read would do, add `--dry-run`: each estimate email is listed
   with what it would do (a new job, which existing job it moves, or that it would wait
   to be linked), and nothing is saved. Use it at go-live before
   `check_estimate_mail --days 14`.

## F. Real Square data on staging (optional, read only)

To see real customers and invoices in staging without connecting staging to the real
account. Add `--live --location <production location ID>` to either import, and type the
**production access token** at the hidden prompt. The token is used for that one command
and kept nowhere: not in `.env`, the shell history or the chat. Staging carries on with
the sandbox afterwards.

Get both from developer.squareup.com, signed in to Graphnix's Square account (section A),
the `GRAPHNIX tracker` app switched to **Production**: **Credentials** (access token) and
**Locations** (location ID). A token from an app under a personal Square account reads that
account, not Graphnix's, and the import says the location can't be read.

```bash
docker compose exec web python manage.py import_square_customers --live --location LXXXX --dry-run
docker compose exec web python manage.py import_square_customers --live --location LXXXX
docker compose exec web python manage.py import_square_invoices --live --location LXXXX --latest 3 --dry-run
docker compose exec web python manage.py import_square_invoices --live --location LXXXX --number 000123
```

Store orders, the same way (`--list` first to see the sources):

```bash
docker compose exec web python manage.py import_square_orders --live --location LXXXX --latest 3 --dry-run
docker compose exec web python manage.py import_square_orders --live --location LXXXX --latest 3
```

`import_square_invoices` takes `--latest N` (20 at most), `--number <Square invoice
number>` or `--id <invoice ID>`, each repeatable. Real invoices have no G- number, so they
wait under **Invoices**: link each to a test job, or **Create job**, to see its payment
status arrive. Estimates can't be imported: Square's API doesn't offer them. They arrive by
email instead (see **Estimates**).

Staging then holds real names, emails and phone numbers. Treat it like production:
staff sign-in only, the same backups.

### Clearing it when testing is done

```bash
docker compose exec web python manage.py clear_square_data
docker compose exec web python manage.py clear_square_data --yes
```

The first only shows what would go. `--yes` removes everything that came from Square,
sandbox included: customers linked to Square or made from it (with their contacts,
addresses and jobs), jobs made for store orders and estimates, every stored invoice, order,
estimate and estimate email, and the audit entries about them. Staff's own customers and
jobs stay; one that was linked to an invoice or estimate is unlinked and its payment reset.
Square itself isn't touched. It refuses to run where `APP_ENV=production`.

While staging still reads `info@` (`ESTIMATE_MAILBOX`), new estimate emails keep arriving,
and the last day's are read again on the next check. Remove `ESTIMATE_MAILBOX` from
staging's `.env` first if it should stay empty.

## When something's wrong

1. The health panel names most problems.
2. **Admin › Square › Webhook events** lists every delivery and its error, if any.
3. A refused signature is logged on the server as "Square webhook refused".
