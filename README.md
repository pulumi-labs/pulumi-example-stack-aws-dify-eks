# Private Dify document workflows on Amazon EKS with Python Pulumi

Build an internal document app: upload a small text file, search its contents,
and use the result in a Dify workflow. The worked example calculates **21 mapped
units** and returns the text it found.

This example adds Dify to an **existing Amazon EKS cluster**. EKS is AWS's managed
Kubernetes service; Kubernetes runs the application's containers. Pulumi creates
and connects the database, file storage and application using Python.

## Before you begin

This is an application example for a prepared platform, not a guide to creating
your first Kubernetes cluster. Ask your platform administrator for:

- An EKS cluster in `us-east-2` that meets the [platform checklist](#platform-checklist).
- Access to deploy into its dedicated Dify namespace and create this app's AWS resources.
- A Tailscale network, which lets your team reach Dify privately without a public website.
- An approved isolated deployment environment with Python 3.12, the pinned Pulumi
  tools and an encrypted state backend. State is Pulumi's record of the resources
  it manages; keep it outside Git.

Start with [step 1](#1-get-the-example), then configure, deploy and try the document
workflow. Read the cleanup section before creating paid resources. Advanced
platform and recovery details are expandable below.

**Deployment smoke test and cleanup passed (2026-11-13).** Broader operational
validation remains partial, not production-ready. The exact standalone runtime
deployed successfully and passed authenticated Knowledge indexing/retrieval and
native five-node Workflow checks: Code returned **21** and Template rendered the
retrieved document. Both native node readbacks matched the prescanned payloads;
original document download integrity was also verified. Native Workflow run IDs,
readbacks and existing raw responses were retained privately in encrypted storage.
Independent cleanup verified zero owned test resources.

Agent success, independent scaling, replacement persistence, coherent restore and
the full TLS matrix remain untested. Six API-client TLS cases passed, not whole-plugin
TLS validation. Complete SSE capture remains incomplete. These limits do not reopen
the completed deployment smoke test or establish complete security/compatibility;
future execution still requires applicable malware scans and an approved sandbox.

**Platform requirement:** strict VPC CNI policies alone do not meet this example's
node-local isolation requirement. Standard Kubernetes
[NetworkPolicy permits traffic to and from a pod's own node](https://kubernetes.io/docs/concepts/services-networking/network-policies/);
provide and verify node-local enforcement separately. Application acceptance is partial.

## What gets deployed

- Dify Community **1.19.0**: API and web Deployments default to two replicas each;
  Celery workers default to one; Beat stays a singleton. Native image entrypoints
  and a one-shot migration Job preserve the upstream startup/queue behavior.
- Private, encrypted **PostgreSQL 15.18**, `db.t4g.small`, 20 GiB gp3, single AZ,
  seven-day automated backups, separate non-superuser application/plugin owners.
  The master password is supplied only to database initialization.
- A versioned, encrypted, public-blocked S3 bucket for files **and tenant RSA
  keys**. Bucket-scoped IRSA is projected only into API, worker, Beat and migration
  pods; no static AWS keys or application S3 rights on the node role.
- Redis, Weaviate, plugin storage and Agent home/workspace on five encrypted RWO
  PVCs totaling **50 GiB**. These singleton services have AZ-bound attach/detach
  downtime: this is **not whole-application HA**.
- nginx behind private Tailscale HTTPS; default-deny pod policies and separate
  Code/Agent Squid proxies with narrow internal exceptions. No cluster, NAT,
  public load balancer, NodePort, Funnel, Helm installation or host installer.

Tailscale controls connectivity, not Dify login or workspace authorization.
Trust workspace editors and reviewed plugins; this is not hostile-code hosting.
Public HTTPS egress is IP-based, **not an FQDN allowlist**. Persistence and backup
creation alone do not establish recoverability.

## Platform checklist

<details>
<summary>For your platform administrator: exact network, storage and access requirements</summary>

A namespace groups this application's Kubernetes resources. RDS is AWS's managed
PostgreSQL database; S3 stores files. IRSA gives selected application pods AWS
permissions without storing permanent AWS keys. CIDRs are the IP address ranges
used by the network rules.

Have the platform owner verify these before configuring the app:

- **EKS 1.35 in `us-east-2`**, exact account/VPC/OIDC identity, IPv4 networking,
  **public API disabled and private API enabled**. The deployment environment
  already needs private endpoint routing and DNS. This stack never opens the API
  or supplies a public-endpoint fallback.
- Selected existing managed nodegroups with Linux **AMD64** nodes, at least two
  schedulable hostnames, approximately 8 allocatable vCPU / 16 GiB plus rolling
  headroom. API/web spread can leave pods Pending if the pool is insufficient.
  All primary/secondary node ENIs must lack public IPv4/EIP and globally routable
  IPv6, including autoscaled/replacement nodes. Check selector overlap and actual
  ENI inventory, not just subnet auto-address flags. Existing approved outbound
  connectivity must support image pulls, S3/STS, Tailscale and provider/plugin HTTPS.
- VPC CNI **1.21+**, kernel **5.10+**, network policy enabled in **strict startup**
  mode, appropriate CoreDNS policies, IMDSv2/hop limit 1 and no node-role S3 access.
  Prove effective Code/Agent denial of metadata, private EKS and same-node/other-node
  management paths. NetworkPolicy/SG objects alone do not prove node-local denial;
  node SGs must not admit app/proxy management traffic, including broad self-SG
  rules. HTTP 401/403 or TLS errors mean a destination was reached, not denied.
- Healthy, version/image-pinned EBS CSI with its own scoped IAM role and an existing
  encrypted **gp3 / WaitForFirstConsumer / Retain** StorageClass. Verify driver
  fsGroup/mount-group behavior on fresh **and restored** volumes: Agent requests
  gid **1000**, plugin storage gid **1001**, both `OnRootMismatch`. Agent's nonroot
  initializer copies three image shell skeleton files only into an empty home;
  existing homes are preserved. These specs are not proof of CSI compatibility.
- Two private RDS subnets in different AZs, verified routing and exact CIDRs.
  Confirm PostgreSQL 15.18/class availability, storage/IP/attachment quotas and
  capacity in the selected pool/AZs.
- Precreated namespace **`dify-team`** for stack `team`. A STANDARD EKS access entry
  maps the deployment role only to `dify-team-deployers`; its namespace Role permits
  CRUD/watch of Deployments, ReplicaSets, StatefulSets, Services, Endpoints,
  ConfigMaps, Secrets, ServiceAccounts, PVCs, Jobs, Ingresses and NetworkPolicies,
  plus pod read/log/exec for diagnostics. Also grant cluster-scoped **`get` only on
  the selected StorageClass**, using `resourceNames`; Pulumi reads its binding mode
  before allowing a `WaitForFirstConsumer` PVC's consumer to start. No cluster-admin
  or namespace/RBAC/CRD/addon management. Platform resources remain separately owned.
- Existing **Tailscale Operator 1.104.4**, official chart and pinned operator/proxy
  images from `pins.json`. Separately provision `operator-oauth` with `client_id`
  and `client_secret`, leaving chart OAuth values empty. Scope Devices Core/Auth
  Keys write permissions to `tag:k8s-operator`, owning the intended proxy tag only.
  Disable API-server proxy/impersonation and review cluster-wide operator authority.
- Tailnet MagicDNS/HTTPS, a unique hostname, `tag:dify` owned by the operator tag,
  and explicit internal-user/admin TCP443 grants without overlapping allow-all.
  No Funnel, exit/subnet routing or Kubernetes API exposure. Require a denied test
  identity; the operator namespace must permit its proxy path to nginx8080.

Use short-lived, scoped AWS credentials for stack-owned S3/RDS/SG/subnet-group/IAM
resources and read-only EKS/nodegroup/VPC/subnet metadata. IRSA creation needs an
approved same-account permissions boundary. Kubernetes authentication uses
`aws eks get-token` against the selected endpoint/CA, not an exported bearer token.
AWS CLI and kubectl are required. Keep provisioning credentials away from app pods.

The Python lock supports **CPython 3.12 on ARM64 macOS or ARM64 Linux glibc**,
not AMD64 Python runners; application nodes are AMD64. Use Pulumi CLI/SDK
**3.273.0**, AWS SDK/provider **7.52.1**, Kubernetes SDK/provider **4.34.2** and an
encrypted [Pulumi backend](https://www.pulumi.com/docs/iac/concepts/state-and-backends/).
Application image digests and the RDS CA hash are in `pins.json`; they are not
automatic updates or evidence of safety.

</details>

## 1. Get the example

```sh
git clone https://github.com/pulumi-labs/pulumi-example-stack-aws-dify-eks.git
cd pulumi-example-stack-aws-dify-eks
```

Run the following steps from the approved deployment environment, in the cloned
directory. Complete the platform checklist before installing dependencies.

## 2. Configure your deployment

A Pulumi **stack** is one named copy of the deployment. These commands create a
Python environment and a stack named `team`; they do not deploy resources yet.
Install only the reviewed dependencies in your approved environment:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
pulumi login # or: pulumi login <your-encrypted-backend-url>
pulumi stack init team
```

Merge this nonsecret configuration into **`Pulumi.team.yaml`**, replacing every
uppercase placeholder with the exact platform inventory. Arrays/maps below use
JSON syntax inside the YAML stack file. Do not overwrite existing encrypted secret
entries. Use a lowercase DNS-safe stack name, at most 31 characters; changing it
also changes the namespace and required proxy labels.

```yaml
config:
  dify-private-eks-python:accountId: "YOUR_12_DIGIT_ACCOUNT"
  dify-private-eks-python:region: us-east-2
  dify-private-eks-python:clusterName: YOUR_EXISTING_CLUSTER
  dify-private-eks-python:vpcId: vpc-REPLACE
  dify-private-eks-python:nodeSecurityGroupId: sg-REPLACE
  dify-private-eks-python:privateSubnetIds: ["subnet-REPLACE_A", "subnet-REPLACE_B"]
  dify-private-eks-python:rdsSubnetCidrs: ["RDS_SUBNET_A_CIDR", "RDS_SUBNET_B_CIDR"]
  dify-private-eks-python:vpcCidrs: ["ALL_ASSOCIATED_VPC_IPV4_CIDRS"]
  dify-private-eks-python:podCidrs: ["EXACT_POD_IPV4_CIDRS"]
  dify-private-eks-python:nodeGroupNames: ["YOUR_MANAGED_NODEGROUP"]
  dify-private-eks-python:nodeSubnetIds: ["subnet-REPLACE_NODE"]
  dify-private-eks-python:nodeCidrs: ["EXACT_NODE_SUBNET_CIDRS"]
  dify-private-eks-python:serviceCidr: EXACT_EKS_IPV4_SERVICE_CIDR
  dify-private-eks-python:privateEndpointCidrs: ["EXACT_PRIVATE_CONTROL_PLANE_DESTINATION_CIDRS"]
  dify-private-eks-python:storageClass: YOUR_ENCRYPTED_GP3_RETAIN_CLASS
  dify-private-eks-python:permissionsBoundaryArn: arn:aws:iam::YOUR_12_DIGIT_ACCOUNT:policy/YOUR_BOUNDARY
  dify-private-eks-python:oidcIssuer: https://oidc.eks.us-east-2.amazonaws.com/id/YOUR_ISSUER_ID
  dify-private-eks-python:nodeSelector: {"eks.amazonaws.com/nodegroup": "YOUR_MANAGED_NODEGROUP"}
  dify-private-eks-python:hostname: dify-team
  dify-private-eks-python:tailnetDnsName: YOUR_TAILNET.ts.net
  dify-private-eks-python:operatorNamespace: YOUR_OPERATOR_NAMESPACE
  dify-private-eks-python:operatorProxyLabels: {"tailscale.com/managed": "true", "tailscale.com/parent-resource": "dify", "tailscale.com/parent-resource-ns": "dify-team", "tailscale.com/parent-resource-type": "ingress"}
  dify-private-eks-python:platformEvidenceSha256: YOUR_REVIEWED_EVIDENCE_64_LOWERCASE_HEX_HASH
```

Use this table with your platform administrator to fill in the values. Do not
guess subnet IDs or network ranges. An AWS ARN is a resource's full identifier.

| Inputs | Exact selection or constraint |
| --- | --- |
| `accountId`, `region`, `clusterName`, `vpcId`, `oidcIssuer` | Expected existing cluster identity; only `us-east-2` and EKS 1.35 are accepted. |
| `nodeSecurityGroupId` | Existing selected node SG trusted by RDS on 5432; this also trusts other platform pods using that SG, not just Dify. |
| `privateSubnetIds`, `rdsSubnetCidrs` | Exactly two distinct private RDS subnets and matching IPv4 CIDRs in different AZs. |
| `vpcCidrs`, `podCidrs` | All associated VPC IPv4 ranges and exact pod ranges, not guessed defaults. |
| `nodeGroupNames`, `nodeSubnetIds`, `nodeCidrs` | One to eight managed groups, their complete subnet-ID set and matching CIDRs. |
| `serviceCidr`, `privateEndpointCidrs` | Exact EKS IPv4 Service range and private endpoint/control-plane destinations within verified control-plane subnets, reconciled to ENI/DNS inventory. |
| `storageClass`, `permissionsBoundaryArn` | Verified encrypted Retain class and same-account IRSA boundary policy. |
| `nodeSelector` | Labels matching every selected group and no unverified pool; program also enforces Linux/AMD64. |
| `hostname`, `tailnetDnsName` | Unique short name and exact lowercase `name.ts.net` suffix, without scheme/trailing dot. |
| `operatorNamespace`, `operatorProxyLabels` | Separate operator namespace and labels selecting only this Ingress's proxy, as above. |
| `platformEvidenceSha256` | Hash of reviewed effective CNI/CSI/operator/RBAC, node/ENI/SG/routing and continuing replacement/admission evidence. A hash is not proof or a substitute for checks. |

CIDR arrays must contain unique canonical IPv4 networks, not default routes;
expand each placeholder to the complete actual set. VPC/node/pod/Service/endpoint
ranges feed both proxies' private denies and every public HTTP(S) policy exclusion.
Optional `apiReplicas`, `webReplicas`, `workerReplicas` accept 1..20, default **2/2/1**.
Restore-only `rdsSnapshotIdentifier` and `pvcSnapshots` are explained below.

Enter **all twelve** independent secrets at prompts, never as command arguments:

```sh
for key in dbMasterPassword dbAppPassword dbPluginPassword redisPassword secretKey \
  pluginKey pluginInnerKey sandboxKey weaviateKey agentApiToken \
  agentServerSecretKey agentSandboxToken; do
  pulumi config set --secret "$key"
done
```

Use high-entropy 32..128 printable non-space characters. `dbMasterPassword`
excludes `/`, `@`, `"`; app/plugin DB passwords allow only `A-Za-z0-9_-`.
**`agentServerSecretKey` must be exactly 43 canonical unpadded base64url characters
encoding 32 random bytes**, including canonical final padding bits. For example,
privately generate with Python `secrets.token_urlsafe(32)` and enter at the prompt;
do not use padded base64 or log the value. Other Agent tokens remain independent.
`secretKey` and tenant RSA keys are needed to decrypt saved provider configuration.
Kubernetes Secret base64 is not encryption: admins/deployers can read these values.

### Database certificate requirements

<details>
<summary>Advanced: how the database connection verifies certificates and hostnames</summary>

API and initialization use explicit **verify-full** with the hash-pinned Ohio
RDS CA. The unmodified plugin image uses this **indivisible native tuple** and
the actual RDS DNS hostname:

```text
DB_SSL_MODE=require
PGSSLROOTCERT=system
SSL_CERT_FILE=/etc/rds/rds-ca.pem
```

`require` alone is **not sufficient**. Inspected pgx 5.9.2 promotes this system-root
selection to effective **verify-full**, retaining certificate/hostname validation
and no plaintext fallback. Acceptance still requires reconciliation of the actual
`/app/main` **and** `/app/commandline` binaries, Go/pgx/source provenance and root
inventory, followed by valid-host success and wrong-host/unknown-root/TLS-off/
reconnection negative checks for both processes. None has been proved on this image.

Go loads the pinned PEM **plus normal public root directories**, not an
RDS-exclusive pool. Keep those directories intact and `SSL_CERT_DIR`/`PGSSLMODE`
unset; do not inject DSN overrides, force fallback roots or downgrade TLS. No
custom image is selected. Normal plugin/subprocess/provider HTTPS also needs
verification. Go caches roots per process, so trust changes require restarts;
deleting one PEM does not necessarily remove usable directory roots. CA hash/mount
checks establish configuration integrity, not a successful TLS negative test.

</details>

## 3. Deploy and try a document workflow

`pulumi preview` shows what will change. Review its account, resource list and
costs before running `pulumi up`, which creates billable resources. `kubectl` is
the Kubernetes command-line tool; use it with the selected cluster:

```sh
pulumi preview
pulumi up
kubectl -n dify-team wait --for=condition=complete job/db-init --timeout=600s
kubectl -n dify-team wait --for=condition=complete \
  "job/$(pulumi stack output migrationJob)" --timeout=600s
kubectl -n dify-team rollout status deployment/api --timeout=900s
kubectl -n dify-team rollout status deployment/web --timeout=900s
pulumi stack output url
```

The database setup and migration jobs must finish before the app can start. The
rollout commands should finish successfully, and the last command prints Dify's
private URL. Open it from a device allowed by your Tailscale policy. A running
app is the starting point; the following document workflow checks useful behavior.

1. Open the output URL from an allowed tailnet client. Verify trusted HTTPS,
   complete first-login setup at `/install`, and retain one internal workspace and
   Dify branding. Check denied/off-tailnet identities cannot connect and
   unauthenticated Dify API calls are rejected.
2. If needed, install an exact reviewed signed model/embedding plugin and configure
   scoped provider credentials privately in Dify, not Pulumi. Keep signature checks
   enabled. Plugins/dependencies/generated payloads execute code and need review
   before installation; do not treat a signature as a safety guarantee.
3. Create Knowledge with Economical indexing; upload your own nonsensitive text:
   `The Aurora archive has marker AURORA-7319. It stores exactly seven blue maps.`
   Wait up to ten minutes for completed indexing, then query `AURORA-7319` and
   check that the original text is retrieved.
4. Create **Start(query) → Knowledge Retrieval → Code → Template → End**. Bind
   retrieval to `query`; Code uses `def main(): return {"maps": 7 * 3}` with numeric
   output `maps`. Bind Template's `maps` to Code.maps and its retrieval context to
   the retrieval result; render `AURORA-7319 contains {{ maps }} mapped units.`
   plus that context. Publish/run it and assert **21**, the marker and retrieved
   text, not just HTTP success. Check normal files/streaming, permitted ordinary
   HTTPS Code requests and authenticated Agent behavior too.

The plugin-free retrieval/Code/Template API slice passed; the full walkthrough,
including browser setup, model/plugin calls and Agent behavior, remains unvalidated.
Model/plugin calls may transmit documents externally and incur fees. Use separate
least-privilege Knowledge/Workflow API keys if automating requests; never use AWS
provisioning credentials or export tokens/resolved Secrets into logs.

## Optional: scale indexing workers

Workers process background jobs such as document indexing. This section describes
an additional experiment; independent scaling was not part of the passed smoke test.

```sh
pulumi config set workerReplicas 2
pulumi up
kubectl -n dify-team rollout status deployment/worker --timeout=900s
kubectl -n dify-team get deployment api web worker beat
```

Submit several indexing/workflow jobs and check **both** workers consume them,
API/web stay 2/2 and Beat stays 1. There is no HPA or metrics-server assumption.
Preserve the native complete Community queue list. Worker shutdown grace is 600s,
not a guarantee for longer jobs. During a platform-coordinated replacement test,
recheck login, provider-secret decryption, file bytes and workflow output after
stateless pods move to another node; never drain/delete shared nodes for this app.

Code uses only its general proxy; Agent local sandbox uses its dedicated proxy
and backend channel. Neither gets AWS/Kubernetes tokens, host networking or sockets.
Keep exact sandbox8194 `/v1/sandbox/`, backend5050 `/agent-stub/` and API5001
`/files/` exceptions **before** generic private/port denies. Verify intended paths
and bounded metadata/private/management denial, including DNS aliases, redirects,
CONNECT, strict startup and replacement-node transitions. Do not disable normal
Code/Template/Agent functionality to conceal a boundary failure.

## Advanced: upgrades, backup and recovery

<details>
<summary>Read before upgrading or attempting a restore; restore remains untested</summary>

**Do not use `pulumi up` as an online schema-upgrade tool.** Disable user ingress,
stop producers/Beat, drain queues with an observed bounded deadline, then stop
API/workers/plugin/Agent writers. Record images/schema and make a coherent recovery
point. Scale all DB users to zero through deliberate maintenance actions before
running the next revision's migration once. The initial dependency graph does not
stop old consumers for you. On failure keep maintenance/consumers down and restore
the matching recovery point; image downgrade is not schema rollback.

For a coherent backup, use separately authorized operator actions while all writers
are quiesced:

1. Snapshot the RDS instance from `pulumi stack output databaseIdentifier` and wait
   for availability (`aws rds create-db-snapshot`, `aws rds wait db-snapshot-available`).
2. Cleanly stop Redis/Weaviate/plugin/local-sandbox StatefulSets. Map each PVC to
   PV/CSI volume ID/AZ and create/wait for completed EBS snapshots
   (`aws ec2 create-snapshot`, `aws ec2 wait snapshot-completed`). Capture all five:
   `redis-data` 5 GiB, `weaviate-data` 20 GiB, `plugin-data` 10 GiB,
   `local-sandbox-home` 5 GiB, `local-sandbox-workspace` 10 GiB.
3. Record **all paginated** S3 keys, VersionIds and delete markers from the bucket
   output `bucketName`. Back up all DB/application/service secrets encrypted outside
   this repository, with image/schema pins and the RDS/EBS/S3 inventories as one
   recovery manifest. Snapshots contain sensitive data. Automated RDS backups and
   S3 versioning alone are not a coherent recovery point.

Restore into a **new stack/namespace/hostname**, keeping originals intact:

1. Platform owner prepares namespace/access and ready preprovisioned CSI
   VolumeSnapshots from all five recorded EBS snapshot handles, with correct
   driver/class/AZ/size. This does not grant the app snapshot/CRD administration.
2. Reuse original encrypted secrets and exact recovery-point images/schema. Set
   `rdsSnapshotIdentifier` to the RDS snapshot ID/ARN and `pvcSnapshots` to a JSON
   map from **all five claim names above** to ready VolumeSnapshot names in the
   new namespace. Partial sets fail closed. Preserve the original master password.
3. Inspect a targeted preview/update listing exact **AWS data-prerequisite URNs
   only**, with no Jobs/consumers/ingress and no `--target-dependents`. First create
   the restored RDS and new bucket/identity prerequisites, without starting writers.
4. Copy each recorded live S3 version into the new bucket with a version-specific
   `s3api copy-object`; respect delete markers, pagination and compare bytes/hashes.
   `aws s3 sync` does not restore a versioned point in time. Preserve tenant RSA
   keys and original `SECRET_KEY`, not fresh replacements.
5. Inspect bound restored PVC/volume identities, then run the full reviewed update
   against the **new stack only**. Initialization verifies DB ownership and sets
   configured owner passwords. Check original login, decrypted provider settings,
   exact document/file bytes, retrieval and Code/Template output before cutover.

Restore has **not been tested**. Pod readiness or snapshot availability is not
recovery. Secret rotation also needs maintenance and deliberate consumer restarts;
Secret environment changes do not restart pods. Encryption-key replacement is not
ordinary credential rotation, and subPath file mounts do not hot-reload.

</details>

## 4. Understand costs and clean up

Budget for existing EKS control plane/nodes/egress, RDS compute and 20 GiB storage,
50 GiB application EBS, snapshots/backups, S3 versions/requests/transfer, Tailscale
seats and model/plugin fees. Recovery temporarily duplicates data resources. Check
current [EKS](https://aws.amazon.com/eks/pricing/), [RDS](https://aws.amazon.com/rds/postgresql/pricing/),
[EBS](https://aws.amazon.com/ebs/pricing/) and [Tailscale](https://tailscale.com/pricing)
prices. No hard cost cap or automatic shutdown is included. Scaling pods to zero
does not stop these charges; stopped RDS restarts automatically after seven days.

- **Pending pods/PVCs:** inspect selected node pool/AZ/capacity/CSI permissions and
  Retain class. Never substitute ephemeral storage or format an existing volume.
- **IRSA AccessDenied:** check issuer, SA subject/audience, boundary, bucket and STS
  path. Do not inject static keys.
- **TLS/proxy/ingress failures:** inspect CA hash, full native tuple/binary/root
  inventory, exact proxy rule ordering and grants/DNS/cert/operator state. Never
  weaken verification, open the EKS API, enable Funnel or expose debug5003.

### Remove this deployment

Deleting a deployment can permanently delete its documents and database. First
choose what to keep. Then back up, disable ingress, drain queues, stop writers and record
stack-owned resources. **RDS, bucket and PVC protection intentionally block a
blanket destroy.** With explicit data-deletion approval, identify exact owned URNs,
prepare a reviewed change disabling RDS deletion protection and choosing a
collision-free final snapshot identifier, apply it, then unprotect only the exact
data resources selected for deletion:

```sh
pulumi stack --show-urns
pulumi state unprotect 'EXACT_APPROVED_OWNED_DATA_RESOURCE_URN'
# Repeat only for each approved owned protected resource; do not apply again.
pulumi destroy
```

S3 `force_destroy` stays false: separately remove all versions/delete markers and
multipart uploads before deleting the bucket, or deliberately retain it.
**Deleting a retained storage claim does not delete its disk or stop disk charges.**
PVC is the application's storage claim, PV is the allocated Kubernetes volume,
and EBS is the underlying AWS disk. The platform owner must reconcile
owned volumes, CSI/EBS snapshots, RDS final/manual/automated snapshots and S3 data,
plus IAM/SG/subnet-group resources. Delete this Ingress and let the existing operator
clean its proxy; independently verify tailnet device removal. Never delete the
existing cluster, operator, OIDC, CNI/CSI, access entry or shared RBAC. Only the
platform owner removes dedicated namespace/access after app cleanup. Keep encrypted
state until all residual resources and continuing charges are reconciled.

## Contributions and support

Use repository issues for reproducible, non-sensitive bugs and deployment questions,
and pull requests for focused improvements. Include the pinned versions, expected
behavior and a sanitized description of the failure. Never attach credentials,
Pulumi state, kubeconfig, private documents or raw application logs. Report security
concerns through the organization's approved private channel; do not open a public
issue containing an exploit or secret. This example does not provide a production
support commitment.

## License and references

This example's authored code is licensed under the [MIT License](LICENSE) and
is provided as-is, without warranty or a support commitment. Third-party
components retain their own licenses and notices.

Preserve [DIFY-LICENSE](DIFY-LICENSE), including modified Apache terms for Dify and
derived nginx/Squid configuration, branding and multi-workspace/multitenant
restrictions. This targets one internal Community workspace, not Enterprise or a
multi-tenant hosted service. Dependency/container licenses remain their publishers'.

[Pinned Dify source](https://github.com/langgenius/dify/tree/8e689e868b63b6dd2b449d0eb568ca5cc7127759),
[EKS strict network policy](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy-configure.html),
[Tailscale Ingress](https://tailscale.com/docs/kubernetes-operator/ingress),
[pgx TLS implementation](https://github.com/jackc/pgx/blob/v5.9.2/pgconn/config.go),
[Pulumi Python](https://www.pulumi.com/docs/iac/languages-sdks/python/).
