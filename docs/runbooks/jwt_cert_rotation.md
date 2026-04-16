# Runbook: Salesforce JWT Bearer Cert Rotation (AD-25)

**Owner:** SRE on-call
**Cadence:** quarterly (forced rotation) + 30 days before any monitored expiry
**Why this exists:** Salesforce sends *no warnings* before a Connected App's JWT certificate expires. When the cert expires, every server-to-server integration that uses it fails simultaneously and silently — Salesforce returns `invalid_grant: invalid assertion` and the integration just stops working. This runbook prevents that outcome by rotating cleanly on a known schedule.

## Trigger conditions

Rotate when **any** is true:
1. Quarterly calendar reminder (1st of Jan / Apr / Jul / Oct).
2. Cert expiry monitor reports < 30 days remaining (alert fires from `mcp.gateway.cert_expiry_days < 30`).
3. Suspected compromise (e.g., key material handled outside Key Vault).
4. Personnel departure with prior access to the private key.

## Pre-flight checks

```bash
# 1. Confirm current Connected App and key path.
echo "$SF_CLIENT_ID"  # Connected App consumer key
echo "$SF_JWT_KEY_PATH"

# 2. Read current cert expiry from the deployed PEM.
openssl x509 -in "$SF_JWT_KEY_PATH" -noout -enddate

# 3. Confirm Key Vault access from this shell.
az keyvault secret show --vault-name <vault> --name sf-jwt-key --query attributes
```

## Rotation procedure

### Step 1 — Generate new keypair

```bash
# 4096-bit RSA, no passphrase (Salesforce JWT bearer flow does not support passphrase).
openssl req -newkey rsa:4096 -nodes -keyout sf_jwt.new.pem \
    -x509 -days 365 -out sf_jwt.new.crt \
    -subj "/CN=offramp-${ORG_ALIAS}-$(date +%Y%m%d)"
```

### Step 2 — Upload public cert to the Salesforce Connected App

1. Setup → App Manager → find the Connected App → Edit.
2. Under "Use digital signatures" → upload `sf_jwt.new.crt`.
3. Save. Salesforce **keeps both certs valid simultaneously** during the transition window — DO NOT delete the old cert yet.

### Step 3 — Stage new private key in Key Vault

```bash
az keyvault secret set --vault-name <vault> --name sf-jwt-key-new \
    --file sf_jwt.new.pem
```

DO NOT overwrite `sf-jwt-key` yet — that is the current production key.

### Step 4 — Smoke test in sandbox

```bash
# Use the dedicated sandbox-rotation test (Phase 0.11 deliverable).
uv run pytest tests/integration/test_cert_rotation.py -v --new-key-path=./sf_jwt.new.pem
```

Test must verify:
- Token issuance succeeds with the new key
- A `sf_query` call lands and returns expected records
- The Engram anchor for the call records the new cert thumbprint

If any check fails: STOP. The new cert is bad. Re-generate (Step 1) or escalate to Salesforce admin.

### Step 5 — Promote new key to production

```bash
# Atomic swap: rename then update the Kubernetes secret reference.
az keyvault secret set --vault-name <vault> --name sf-jwt-key \
    --file sf_jwt.new.pem
kubectl rollout restart deployment/mcp-gateway -n offramp-${CUSTOMER}
```

Watch for the rollout to complete and `mcp.gateway.auth_success_rate` to stay at 100%. If it drops, roll back: re-upload the prior PEM into `sf-jwt-key` and restart.

### Step 6 — Decommission the old cert (after 24h soak)

After 24 hours of clean operation on the new cert:

1. Salesforce Setup → Connected App → remove the old cert from the digital signatures list.
2. Delete `sf-jwt-key-old` from Key Vault.
3. Securely shred any local copies of the prior PEM.

### Step 7 — Update monitoring

```bash
# Re-arm the expiry monitor against the new cert's expiry date.
uv run offramp ops set-cert-expiry-watch --org "$SF_ORG_ALIAS" \
    --pem "$SF_JWT_KEY_PATH" --warn-days 30
```

## Rollback

If post-rotation Salesforce calls start failing:

```bash
# 1. Restore the prior PEM.
az keyvault secret set --vault-name <vault> --name sf-jwt-key \
    --file ./sf_jwt.prior.pem
kubectl rollout restart deployment/mcp-gateway -n offramp-${CUSTOMER}

# 2. Confirm recovery.
uv run pytest tests/integration/test_cert_rotation.py::test_current_cert_works
```

Both certs are valid in Salesforce until Step 6, so rollback is a key swap, not a Salesforce operation.

## Audit checklist (every rotation)

- [ ] Old cert expiry date, new cert expiry date, rotation date all recorded
- [ ] Engram anchor of the cert thumbprint change is captured
- [ ] On-call rotation log entry written
- [ ] Next quarterly reminder confirmed in calendar
- [ ] If rotation was driven by suspected compromise: incident ticket linked

## Related

- Component: `src/offramp/mcp/server.py` (auth path)
- Test: `tests/integration/test_cert_rotation.py` (added in Phase 0.11)
- Architecture decision: AD-25 in [docs/architecture.md](../architecture.md)
