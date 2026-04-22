# Runbook: Connect a real Salesforce scratch org

**Owner:** any engineer wiring up the first real-org connection
**When:** first scratch-org integration test, OR first paid X-Ray engagement

Once the steps below are done, the MCP gateway authenticates to the org via JWT bearer flow on every call — no interactive logins, no refresh tokens.

## Prereqs

- `sf` CLI installed (`brew install sfdx-cli` on macOS; otherwise [sfdx install guide](https://developer.salesforce.com/tools/salesforcecli))
- A Salesforce **Developer Edition** org, OR an existing Dev Hub to spawn scratch orgs from
- `openssl` (for keypair generation)

## Step 1 — Generate the RSA keypair

```bash
mkdir -p ~/secrets/offramp
cd ~/secrets/offramp

# 2048-bit RSA, no passphrase (SF JWT-bearer doesn't support passphrase)
openssl req -newkey rsa:2048 -nodes -keyout sf_jwt.pem \
    -x509 -days 365 -out sf_jwt.crt \
    -subj "/CN=offramp-scratch-$(date +%Y%m%d)"

chmod 600 sf_jwt.pem
```

Two files produced:
- `sf_jwt.pem` — private key (goes on the Off-Ramp host only)
- `sf_jwt.crt` — public cert (uploaded to Salesforce in Step 3)

## Step 2 — Create the scratch org

If you already have a scratch org, skip to Step 3 and substitute its alias.

```bash
# Auth to your Dev Hub (one-time; opens a browser)
sf org login web --alias DevHub --set-default-dev-hub

# Create a scratch org (7-day lifetime, adjust --duration-days for longer)
sf org create scratch --alias offramp-scratch \
    --definition-file config/project-scratch-def.json \
    --duration-days 7 --set-default
```

Record the scratch-org username:

```bash
sf org display --target-org offramp-scratch --json \
    | jq -r '.result.username'
# -> integration@offramp-scratch.example.com
```

## Step 3 — Create the Connected App

This is a Salesforce-side UI task (no CLI for Connected App creation yet).

1. Open the scratch org: `sf org open --target-org offramp-scratch`
2. Navigate: **Setup → App Manager → New Connected App**
3. Basic Information:
   - Connected App Name: `Offramp Integration`
   - API Name: `Offramp_Integration`
   - Contact Email: (any)
4. API (Enable OAuth Settings):
   - **Enable OAuth Settings**: checked
   - **Callback URL**: `http://localhost:1717/OauthRedirect` (any valid URL works — JWT flow doesn't redirect)
   - **Use digital signatures**: checked
   - **Upload** the `sf_jwt.crt` from Step 1
   - Selected OAuth Scopes: `api`, `refresh_token`, `offline_access`, `full`
5. Save. Wait 2-10 minutes for the app to propagate.
6. Click **Manage → Edit Policies**:
   - **Permitted Users**: `Admin approved users are pre-authorized`
   - **IP Relaxation**: `Relax IP restrictions`
7. Save.
8. Grant the integration user pre-authorization: **Manage Profiles → System Administrator** (or a dedicated Integration User profile).
9. Copy the **Consumer Key** from Manage → View. You'll need it below as `SF_CLIENT_ID`.

## Step 4 — Configure Off-Ramp

Edit `.env` (gitignored):

```bash
SF_ORG_ALIAS=offramp-scratch
SF_LOGIN_URL=https://login.salesforce.com   # or test.salesforce.com for sandboxes
SF_CLIENT_ID=<Consumer Key from Step 3.9>
SF_USERNAME=<username from Step 2>
SF_JWT_KEY_PATH=/Users/you/secrets/offramp/sf_jwt.pem
SF_API_VERSION=66.0
```

## Step 5 — Smoke test the connection

```python
# One-shot JWT exchange via the library
uv run python -c "
import asyncio
from offramp.core.config import get_settings
from offramp.mcp.jwt_auth import session_id

async def main():
    settings = get_settings().salesforce
    access, instance = await session_id(settings)
    print(f'instance_url: {instance}')
    print(f'access_token: {access[:12]}...{access[-4:]}')

asyncio.run(main())
"
```

Expected output:
```
instance_url: https://<your-scratch-org>.my.salesforce.com
access_token: 00D000...AAAA
```

If you see `invalid_grant: user hasn't approved this consumer`:
- The Connected App still hasn't propagated — wait a few minutes.
- OR the integration user's profile isn't pre-authorized (Step 3.8).

If you see `invalid_client_id`:
- Double-check the `SF_CLIENT_ID` matches the Consumer Key exactly.

## Step 6 — Full pipeline against the real org

Once Step 5 works, every existing CLI command targets the real org when invoked with `--org` instead of `--fixture`:

```bash
# Extract the real org's automation surface
uv run offramp extract --org offramp-scratch --out out/real_org

# Render the X-Ray report (uses the same real-backend path)
uv run offramp xray --org offramp-scratch --out out/real_org/xray

# Generate translations + deploy them
uv run offramp generate --org offramp-scratch --out artifacts/real_org
```

**Note:** the `--org` flag currently still routes to the stubbed real backends (`SaltoPullClient` etc.). Wiring the `FixturePullClient` path to use the live `SimpleSalesforceBackend` is the next follow-up.

## Step 7 — Cert lifecycle

- See [jwt_cert_rotation.md](jwt_cert_rotation.md) for the quarterly rotation workflow.
- The cert expires on the `-days 365` you set in Step 1. Rotate **before** the expiry.
- If your scratch org expires (7-day default), you'll need a fresh scratch + new Connected App. Dev Editions persist indefinitely.

## Rollback / cleanup

```bash
sf org delete scratch --target-org offramp-scratch
rm -rf ~/secrets/offramp/
```

## Related

- [docs/runbooks/jwt_cert_rotation.md](jwt_cert_rotation.md) — AD-25 rotation runbook
- [src/offramp/mcp/jwt_auth.py](../../src/offramp/mcp/jwt_auth.py) — the actual exchange code
- Salesforce's [JWT Bearer Flow docs](https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_jwt_flow.htm)
