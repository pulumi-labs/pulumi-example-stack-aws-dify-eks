# Maintaining this private Dify EKS example

- `Pulumi.yaml` selects Python; `__main__.py` is the native AWS/Kubernetes Pulumi
  entrypoint. It imports runtime `validation.py` for canonical Agent secret checks,
  reads `pins.json` and every file in `config/`, and mounts SQL/nginx/Squid/CA
  inputs. `requirements.txt` includes `requirements.lock`. Keep this closure
  standalone; do not remove `validation.py` as a test or add unrelated config files.
- Preserve immutable image/CA pins, full wheel hashes, `DIFY-LICENSE` and config
  attribution. Python 3.12 ARM64 macOS/Linux-glibc runner lock differs from AMD64
  application nodes. Keep CLI/SDK/providers aligned at Pulumi 3.273.0, AWS 7.52.1,
  Kubernetes 4.34.2; changed artifacts need renewed compatibility/security checks.
- Use native `pulumi config`, `require_secret` and interactive `set --secret`.
  Preserve all twelve independent keys and the strict 43-character canonical
  unpadded-base64url/32-byte `agentServerSecretKey` check. No state, kubeconfig,
  resolved Secrets, tokens, private documents or logs in the repository. Model
  credentials belong in Dify. DB master belongs only in initialization.
- Existing private-only EKS 1.35/us-east-2 and verified nonpublic managed nodes are
  prerequisites, not stack-created infrastructure. Preserve explicit providers,
  exact CIDR/identity checks, namespace-scoped RBAC contract and effective platform
  evidence. Do not create/mutate shared endpoint/SG/routing, operator, CNI/CSI,
  cluster/RBAC or add a public/Compose fallback to make an incompatible platform work.
- Preserve scoped IRSA for storage consumers only, identity-free Code/Agent pods,
  default-deny strict-startup policies, all management CIDR exclusions and exact
  internal proxy exceptions before generic denies. No static AWS keys, API token,
  host socket/network or public ingress. Tailscale grants do not replace Dify auth.
- API/init retain explicit verify-full. Plugin TLS requires the complete tuple
  `DB_SSL_MODE=require`, `PGSSLROOTCERT=system`,
  `SSL_CERT_FILE=/etc/rds/rds-ca.pem`, actual RDS hostname, pinned CA and normal public
  root directories. Require alone is unsafe. No DSN/env overrides, TLS downgrade or
  speculative custom image. Reconcile both native binaries/Go/pgx and actual root/
  subprocess behavior; restart relevant processes after trust changes.
- Preserve native entrypoints/full queues, singleton Beat and migration ordering.
  Agent gid1000/empty-home seed preservation and plugin gid1001 are component-specific;
  prove fresh/restored CSI writes rather than imposing global UIDs or chmod777.
  Maintain protected RDS/S3/PVCs, Retain storage and original encryption/tenant keys.
  Upgrades require maintenance, queue drain and coherent recovery point before
  stopping all DB users and running one migration. Do not blindly retry schema work.
- Safe review covers Python syntax, exact provider schemas, local-file closure,
  secret propagation, image/CA/lock integrity and rendered policies without secret
  dumps. Preview executes Python/providers; run checks/dependencies only in a
   suitably isolated authorized environment after security review. Deployment smoke
   test and cleanup passed (2026-11-13); broader operational validation remains partial,
   not production-ready, and untested Agent/scaling/persistence/restore/full-TLS checks
   are not prerequisites to completing that already-passed scope.
- Cleanup is deliberate: drain/stop writers, approve exact data deletion, handle
  protection/final snapshots/S3 versions and retained PV/EBS billing explicitly.
  Reconcile owned cloud/tailnet residuals before removing state; never delete the
  preexisting cluster/operator/CNI/CSI/OIDC/access entry/shared RBAC. Keep README
  validation claims honest and do not invent a license for authored code.
