# Requests from the website's contact form

The **Contact Us** form on graphnix.com (GoDaddy) emails each message to `info@graphnix.com`
from `notifications@mail.conversations.godaddy.com`, with the subject "New contact form
message for Graphnix.com via Contact Us". The tracker reads those every 5 minutes and makes
a **request** for each one, under **Requests** in the header:

- channel **Website Form**, status **New**, received at the time of the email;
- the name, email and phone from the form;
- "Describe your project" as the details (its first line as the subject), with the expected
  completion date and any question the form gains later;
- the customer, when exactly one customer has that email. Otherwise pick or create one on
  the request, as for a phoned-in request.

The header shows how many requests are still **New**. From there it's the usual flow: set
the customer, then **Convert to job** or **Decline**.

Only that sender's form emails are opened. Other mail in info@ is listed by sender only and
never opened. Each email makes one request, however often it's read.

## Setting it up

Nothing more than the estimate emails need (Square setup, **Estimates**: the Exchange read
role on info@ and `ESTIMATE_MAILBOX` in `.env`). The form reader uses the same mailbox
unless `WEBFORM_MAILBOX` says otherwise; `WEBFORM_MAILBOX=off` turns it off. As with
estimates, only one server should read it for real: leave it on staging until production
is live, then turn it off there (`WEBFORM_MAILBOX=off`, restart `web` and `worker`).

To read now instead of waiting, or to bring in older forms:

```bash
docker compose exec web python manage.py check_webform_mail
docker compose exec web python manage.py check_webform_mail --days 30
```

The health panel (`/ops/health/`) shows the last read and any error.

If GoDaddy changes the sender address, set `WEBFORM_SENDERS` in `.env` (comma-separated).
