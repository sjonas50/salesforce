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
  sf project deploy start --target-org "$ALIAS" --source-dir tests/integration/fixtures/sample_org \
    --ignore-conflicts --wait 30 --json | tee "$OUT.deploy.json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",{})
print("deploy status:", r.get("status"), "| components:", r.get("numberComponentsDeployed"), "/", r.get("numberComponentsTotal"))
for f in r.get("details",{}).get("componentFailures",[])[:40]:
    print("  FAIL", f.get("componentType"), f.get("fullName"), "-", f.get("problem"))
'
  sf org assign permset --name Sales_User --target-org "$ALIAS" || true
}
xray() {
  uv run offramp xray --org "$ALIAS" --auth sf-cli --out "$OUT" --no-graph-db --skip-annotations
  uv run python scripts/verify_xray.py "$OUT"
}
case "$cmd" in
  create) create ;;
  deploy) deploy ;;
  xray) xray ;;
  all) create; deploy; xray ;;
  *) echo "usage: $0 {create|deploy|xray|all}"; exit 2 ;;
esac
