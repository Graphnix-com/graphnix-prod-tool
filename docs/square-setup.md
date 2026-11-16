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
     `invoice.scheduled_charge_failed`, `customer.created`, `customer.updated`
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
invoice without one is kept, and waits to be linked by hand (the "Link to job" inbox,
coming in week 2).

If nothing arrives: **Admin › Square › Webhook events** lists every delivery and any error.
A refused signature is logged on the server as "Square webhook refused".
