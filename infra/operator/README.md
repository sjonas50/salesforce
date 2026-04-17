# Off-Ramp on-prem operator (Phase 5.9)

For customers with data-residency requirements who run the platform on
their own Kubernetes (no Azure / no AWS managed services).

The operator handles two production concerns the cloud chart can defer to
managed services:

1. **Secrets materialization**: customers run their own KMS / Vault. The
   operator watches `OfframpSecretMount` CRDs and materializes the named
   secret into the path the MCP gateway expects.
2. **CDC ingestion fallback**: when the customer's network blocks outbound
   gRPC to `api.pubsub.salesforce.com:7443`, the operator runs an internal
   bridge using the SOAP Streaming API (CometD) — slower, but works through
   strict egress proxies.

## Layout

```
operator/
├── manifests/         CRD + RBAC + Deployment YAML
├── config/            sample customer configurations
└── README.md          this file
```

## Status

Phase 5 ships the CRDs + manifest skeletons + a documented controller-loop
contract. The Go controller binary lands in a dedicated follow-up so it can
be properly rev-locked against the controller-runtime SDK version.

## Reference

- [Architecture §17.1 deployment model](../../docs/architecture.md)
- [Build plan Phase 5.9](../../docs/build-plan.md)
