# Square setup (Step 2)

The tracker reads Square; it never changes anything there. Until the settings below are in
a server's `.env`, Square is off on that server and the tracker works as in Step 1.

Do staging first, with the Square **sandbox**. Production uses the real account, later.

## A. The Square application (developer.squareup.com, as the Square account owner)

1. **Applications → +** and name it `GRAPHNIX tracker`.
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
   `G-1042 team tees`, and send it.
3. Within a minute the job is at **Invoiced** (if it was still at a quote stage), and its
   Details tab shows the payment status. The timeline shows "Square invoice … linked".
4. Pay it in the sandbox (the invoice page has a test card). The job shows **Paid**.

What staff need to know: **put the job's G- number in the Square invoice title.** Square
gives the invoice its own number, so the G- number is how the tracker finds the job. An
invoice without one waits under **Invoices** in the header (Admin and Manager):
type the job number and **Link**, or **Not a job** for a sale with no production. A wrong
match is fixed with **Unlink** in the job's Payment panel (Details tab).

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

Customers already linked are skipped, so it's safe to run again. After go-live, a new
Square customer whose email matches a tracker customer links itself.

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

The webhook subscription needs `order.created` and `order.updated` (section A). The nightly
check also reads store orders updated in the last 3 days, so a missed webhook still makes
its job.

## F. Real Square data on staging (optional, read only)

To see real customers and invoices in staging without connecting staging to the real
account. Add `--live --location <production location ID>` to either import, and type the
**production access token** at the hidden prompt. The token is used for that one command
and kept nowhere: not in `.env`, the shell history or the chat. Staging carries on with
the sandbox afterwards.

Get both from developer.squareup.com, the `GRAPHNIX tracker` app, switched to
**Production**: **Credentials** (access token) and **Locations** (location ID).

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
wait under **Invoices**: link each to a test job to see its payment status arrive.
Estimates can't be imported: Square's API doesn't offer them. Type the estimate number on
the job instead.

Staging then holds real names, emails and phone numbers. Treat it like production:
staff sign-in only, the same backups. Delete the imported records when you're done
testing.

## When something's wrong

1. The health panel names most problems.
2. **Admin › Square › Webhook events** lists every delivery and its error, if any.
3. A refused signature is logged on the server as "Square webhook refused".
