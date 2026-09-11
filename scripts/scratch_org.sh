#!/usr/bin/env bash
# Create (or reuse) an org, deploy the fixture metadata, and run X-Ray against it.
#
#   scripts/scratch_org.sh create   # needs an authorized Dev Hub: sf org login web --set-default-dev-hub
#   scripts/scratch_org.sh deploy   # push tests/integration/fixtures/sample_org into the org
#   scripts/scratch_org.sh xray     # offramp xray --org $ALIAS --auth sf-cli
#   scripts/scratch_org.sh all
set -euo pipefail
ALIAS="${SF_ORG_ALIAS:-offramp-scratch}"
OUT="${OUT:-out/real_org}"
cmd="${1:-all}"

create() {
  sf org create scratch --alias "$ALIAS" --definition-file config/project-scratch-def.json \
    --duration-days 7 --set-default --wait 15
  sf org display --target-org "$ALIAS"
}
deploy() {
  # AutoResponseRule.senderEmail must be a real address (the deploying user's, or a
  # verified org-wide address). Keep the fixture generic; substitute at deploy time.
  local sender="${SF_SENDER_EMAIL:-$(sf data query --target-org "$ALIAS" --json \
    -q "SELECT Email FROM User WHERE Username = '$(sf org display --target-org "$ALIAS" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])')'" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["records"][0]["Email"])')}"
  local src; src="$(mktemp -d)/sample_org"
  cp -R tests/integration/fixtures/sample_org "$src"
  # Queue email too: an alert addressed to a queue-owned record's "owner" needs it.
  sed -i '' "s|sales@example.com|$sender|g" "$src"/autoResponseRules/*.xml "$src"/queues/*.xml
  # Salesforce no longer lets new orgs create Process Builder processes (processType
  # Workflow); the fixture keeps one for the parser path, but it cannot be deployed.
  grep -l "<processType>Workflow</processType>" "$src"/flows/*.xml | xargs rm -f
  # Deploy in Metadata API format: source-format deploys consult the org's
  # source tracking and silently skip files it believes are unchanged.
  sf project convert source --source-dir "$src" --output-dir "$src.md" --json >/dev/null
  sf project deploy start --target-org "$ALIAS" --metadata-dir "$src.md" \
    --ignore-errors --wait 30 --json | tee "$OUT.deploy.json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",{})
print("deploy status:", r.get("status"), "| components:", r.get("numberComponentsDeployed"), "/", r.get("numberComponentsTotal"))
for f in r.get("details",{}).get("componentFailures",[])[:40]:
    print("  FAIL", f.get("componentType"), f.get("fullName"), "-", f.get("problem"))
'
  sf org assign permset --name Sales_User --target-org "$ALIAS" || true
}
xray() {
  # Load the graph into FalkorDB when one is listening (make falkordb); else in memory.
  local graph_flag="--no-graph-db"
  redis-cli ping >/dev/null 2>&1 && graph_flag=""
  uv run offramp xray --org "$ALIAS" --auth sf-cli --out "$OUT" $graph_flag --skip-annotations
  uv run python scripts/verify_xray.py "$OUT"
}
case "$cmd" in
  create) create ;;
  deploy) deploy ;;
  xray) xray ;;
  all) create; deploy; xray ;;
  *) echo "usage: $0 {create|deploy|xray|all}"; exit 2 ;;
esac
