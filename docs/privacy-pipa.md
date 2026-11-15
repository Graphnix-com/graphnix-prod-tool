# Privacy checklist (Alberta PIPA)

For Jenn and Nick (NFR-09). It lists what the tracker holds about customers, who can see
it, and what Graphnix still has to decide or write down. It is a working checklist, not
legal advice. The Office of the Information and Privacy Commissioner of Alberta
(oipc.ab.ca) publishes plain-language guides for small businesses.

Status: **Done** = built into the tracker in Step 1. **To do** = a task for Graphnix.
**Open** = a decision not made yet.

## What the tracker holds

| Data | Why | Who can see it |
|---|---|---|
| Customer name, company | To know whose job it is | Everyone (Production sees names only) |
| Customer and contact email, phone | To reach them about the order | Admin, Manager, Avery (QA and shipping, Customer entry) |
| Shipping and billing addresses | Delivery and invoices | Admin, Manager, Avery |
| Customer notes | Order history and preferences | Admin, Manager, Avery |
| Inquiries (what they asked for, from name, email, phone) | Quoting | Admin, Manager, Avery |
| Job details, items, artwork folder path | Making the order | Everyone |
| Payment status and amounts (from Square, Step 2) | Billing | Admin, Manager only |
| Staff: name, work email, sign-in times, what they did | Access control and audit log | Admin |

Nothing else is collected: no dates of birth, ID numbers or card numbers. Card payments
stay in Square.

## Checklist

| # | Item | Status |
|---|---|---|
| 1 | **Collect only what the job needs.** Customer records hold name, company, email, phone and addresses, nothing more (spec, Data model). | Done |
| 2 | **Limit who sees it.** Production never sees contact details or money. Search by email is only for roles that see contacts. Tested on every page (`tests/test_no_money.py`, `tests/test_permission_matrix.py`). | Done |
| 3 | **Know who did what.** Every edit, status move, sign-in, refused sign-in and PIN lock is in the audit log, which only Admin can read. | Done |
| 4 | **Secure sign-in.** Office staff use Microsoft 365, and leavers drop off with their group. Shop-floor PINs work only on the registered PC and lock after 5 wrong tries. Sessions end after 12 hours, or 10 idle minutes on the shop-floor PC. | Done |
| 5 | **Protect it in transit and in backups.** HTTPS only (Caddy). Nightly database backups are encrypted with age before they leave the server, and the private key is kept offline in the password manager. | Done |
| 6 | **Opt-out of notifications.** Each customer and contact has a notification opt-out. It's used once customer emails start in a later step. | Done |
| 7 | **Name a privacy contact.** PIPA requires someone at Graphnix to be responsible for personal information and to answer questions about it. Choose who (for example Jenn or Nick) and put their contact on the website and invoices. | To do |
| 8 | **Write a short privacy notice.** What is collected, why, who it's shared with (Square, Microsoft 365, the server host) and how to reach the privacy contact. Customers should be told the purpose when their information is collected (website, quote emails, counter). | To do |
| 9 | **Service providers outside Canada.** PIPA expects the notice and policy to say when a service provider outside Canada handles personal information, and how to ask about it. Check where the server host, Square and Microsoft 365 store Graphnix's data, and say so in the notice. Server provider and region: ________ | To do |
| 10 | **Access and correction requests.** A customer can ask for the information Graphnix holds about them, or ask for a correction. Reply within 45 days. The customer page shows everything the tracker holds; print it or copy it into a reply. | To do (process) |
| 11 | **Breach plan.** If personal information is lost or accessed without permission, and there is a real risk of significant harm, PIPA requires notifying the Commissioner without unreasonable delay, and then the affected people. Josh gets crash and backup alerts. Agree who decides and who notifies. | To do |
| 12 | **Retention.** How long to keep customer records after their last job (keep forever, or delete or anonymise after N years). Step 1 records the policy only; deletion comes later. Backups keep 7 days on the server and 30 days on the shop NAS, so deleted records age out of backups within 30 days. | Open |
| 13 | **Crash emails.** A crash email can include details of the page that failed, which may include a customer's name or email. They go only to Josh's work address and are throttled. Delete them once the problem is fixed. | Done (rule) |
| 14 | **Typed notes.** Job titles, notes and item text are visible to Production. The tracker refuses dollar amounts there. Staff should also leave customer contact details out of those fields; they belong on the customer page. | Done (prices), To do (tell staff) |
| 15 | **Shared PCs.** Sign out with **Done: back to names** on the shop-floor PC and **Sign out** elsewhere. Don't save tracker passwords in shared browsers. | To do (tell staff) |

## When something changes

Re-check this list when the tracker starts emailing customers, adds the proof portal
(Step 2 or later), or a new kind of customer data is added.
