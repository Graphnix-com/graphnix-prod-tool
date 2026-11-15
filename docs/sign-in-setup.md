# Sign-in setup: Microsoft 365 and the shop-floor PC

Step 1, week 4 (INT-07, NFR-04). Do part A once per tenant; part B on each server; part C
on the shop-floor PC.

How it works:

- **Office staff** (Jenn, Nick, McKenna) click **Sign in with Microsoft 365**. Their tracker
  role comes from the Entra security group they are in, read at every sign-in. Take
  someone out of the group, or switch them off in Microsoft 365, and their next sign-in is
  refused.
- **Production staff** have no Microsoft 365 account. They tap their name on the
  shop-floor PC and type a 4-digit PIN. A PIN works only on that PC. 5 wrong tries lock it
  until an Admin sets a new one. After 10 minutes without a click, the PC goes back to the
  names.
- **One local admin account** with a password stays as the fallback for when Microsoft is
  down (`/login/?password=1`).

## A. Entra (entra.microsoft.com, as a Global or Groups + Application admin)

This only adds groups and sign-in settings. Leave GoDaddy's federation settings and the
GoDaddy admin and partner accounts as they are.

1. **Three security groups.** Groups → All groups → **New group**:
   - Group type **Security**, membership type **Assigned**.
   - `Tracker - Admin/Owner`: add Jenn and Nick (and yourself if you want Admin).
   - `Tracker - Manager`: add McKenna.
   - `Tracker - Production`: leave empty. It's there for production staff who get a
     Microsoft 365 account later.

   Open each group and copy its **Object ID**. These IDs aren't secret.

2. **Redirect URIs.** App registrations → **Graphnix Tracker** → Authentication → **Add a
   platform** → **Web**. Add one redirect URI per server, exactly, with the slash at the
   end:
   - `https://staging.jobs.graphnix.com/login/microsoft/callback/`
   - `https://jobs.graphnix.com/login/microsoft/callback/`
   - Optional, for testing on your PC: `http://localhost:8000/login/microsoft/callback/`
     (open the app at `localhost`, not `127.0.0.1`, or Microsoft rejects the address).

   Leave both "implicit grant" boxes unticked.

3. **Groups claim.** Same app → Token configuration → **Add groups claim** → tick
   **Security groups** → under **ID**, choose **Group ID** → Add.

4. **Sign-in permissions.** Same app → API permissions → Add a permission → Microsoft
   Graph → **Delegated permissions** → tick `openid`, `profile` and `email` → Add. Then
   **Grant admin consent for Graphnix**, so nobody gets a consent prompt.
   - These only let the tracker read who signed in. Don't add `Mail.Send` here: sending
     stays limited to `info@` and `tracker-test@` through Exchange RBAC (server setup,
     section 9).
   - If `User.Read` is listed from when the app was created, you can remove it. The
     tracker doesn't use it.

The same client secret is used for sign-in and for sending email, so there's no new
secret.

## B. Each server's `.env`

Add the three group IDs, then redeploy:

```
M365_GROUP_ADMIN=<object id of Tracker - Admin/Owner>
M365_GROUP_MANAGER=<object id of Tracker - Manager>
M365_GROUP_PRODUCTION=<object id of Tracker - Production>
```

The Microsoft button appears once `M365_TENANT_ID`, `M365_CLIENT_ID`,
`M365_CLIENT_SECRET` and at least the Admin or Manager group are set.

Before anyone signs in:

1. In **Admin › Users**, give Jenn, Nick and McKenna their Microsoft 365 email addresses.
   Their first Microsoft sign-in then links to the account `seed_staff` made. Otherwise
   the tracker makes them a new account and the seeded one sits unused.
2. Keep one fallback admin with a password, under an address that isn't anyone's
   Microsoft 365 sign-in (it doesn't need to receive mail):
   ```bash
   docker compose exec web python manage.py createsuperuser
   ```
   Keep its password in the password manager.
3. Test: sign in as McKenna (should land on Home, no Admin link), then as Jenn (Admin
   link shown).

Taking access away:

- Remove the person from their `Tracker -` group, or switch them off in Microsoft 365.
  Their next sign-in is refused.
- A browser that is already signed in stays signed in for up to 12 hours. To end it now,
  untick **Active** on their tracker account in Admin › Users. That takes effect on
  their next click.

## C. The shop-floor PC

1. **PINs.** In Admin › Users, open each production person (Rommel, Isaac, Quentin,
   Avery) and type a 4-digit **New PIN**. Tell each person their PIN in person.
   - A locked PIN shows as "Locked since …" in the user list. Typing a new PIN unlocks
     it.
   - New production staff: Admin › Users › Add. Fill in the name, leave the email empty,
     type a PIN and leave password sign-in **Disabled**.
2. **Register the PC**, on the shop-floor PC itself, in the browser people will use:
   - Go to `https://jobs.graphnix.com/login/?office=1`.
   - Sign in with Microsoft 365 as Jenn or Nick.
   - Open `https://jobs.graphnix.com/shop-floor/setup/` and click **Make this the
     shop-floor PC**.

   You are signed out and the names appear.
3. **Browser settings on that PC**:
   - Make `https://jobs.graphnix.com/shop-floor/` the start page.
   - Make sure the browser doesn't clear cookies when it closes. If it does, the PC
     forgets it's the shop-floor PC, and step 2 has to be done again.
4. **To stop a PC** (it was replaced, or the browser was reset), untick it in
   Admin › Shop-floor PCs.

Avery away from the shop floor: in Admin › Users, give Avery a sign-in address and a
password. The address doesn't need to receive mail (for example
`avery@shop.graphnix.com`). Avery's PIN keeps working on the shop-floor PC.
