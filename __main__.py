"""Dify Community on an explicitly selected, operator-managed EKS platform."""
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import quote

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as k8s

from validation import validate_agent_server_secret

ROOT = Path(__file__).resolve().parent
pins = json.loads((ROOT / "pins.json").read_text())
cfg = pulumi.Config()
stack = pulumi.get_stack()
if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", stack):
    raise ValueError("Use a lowercase DNS-safe stack name, at most 31 characters")
namespace = "dify-" + stack
account = cfg.require("accountId")
region = cfg.require("region")
cluster_name = cfg.require("clusterName")
vpc = cfg.require("vpcId")
node_sg = cfg.require("nodeSecurityGroupId")
subnets = cfg.require_object("privateSubnetIds")
pod_cidrs = cfg.require_object("podCidrs")
vpc_cidrs = cfg.require_object("vpcCidrs")
node_cidrs = cfg.require_object("nodeCidrs")
service_cidr = cfg.require("serviceCidr")
endpoint_cidrs = cfg.require_object("privateEndpointCidrs")
node_subnet_ids = cfg.require_object("nodeSubnetIds")
node_group_names = cfg.require_object("nodeGroupNames")
storage_class = cfg.require("storageClass")
restore_snapshot = cfg.get("rdsSnapshotIdentifier")
pvc_snapshots = cfg.get_object("pvcSnapshots") or {}
if not isinstance(pvc_snapshots, dict) or any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", n) for n in pvc_snapshots.values()):
    raise ValueError("pvcSnapshots must map owned PVC names to precreated CSI VolumeSnapshot names")
boundary = cfg.require("permissionsBoundaryArn")
issuer = cfg.require("oidcIssuer").removeprefix("https://").rstrip("/")
hostname = cfg.require("hostname")
tailnet = cfg.require("tailnetDnsName")
operator_namespace = cfg.require("operatorNamespace")
operator_labels = cfg.require_object("operatorProxyLabels")
selector = cfg.require_object("nodeSelector")
if not re.fullmatch(r"\d{12}", account) or region != "us-east-2":
    raise ValueError("Supply an exact AWS account and supported region us-east-2")
if not re.fullmatch(r"[a-z][a-z0-9-]{0,50}", hostname):
    raise ValueError("hostname must be a unique short DNS label")
if not re.fullmatch(r"[a-z0-9-]+\.ts\.net", tailnet):
    raise ValueError("tailnetDnsName must be your exact MagicDNS ts.net suffix")
if not isinstance(subnets, list) or len(set(subnets)) != 2 or not all(re.fullmatch(r"subnet-[0-9a-f]+", s) for s in subnets):
    raise ValueError("Exactly two distinct private RDS subnet IDs required")
def ipv4_ranges(name, values):
    if not isinstance(values, list) or not 1 <= len(values) <= 64 or not all(isinstance(c, str) for c in values) or len(set(values)) != len(values):
        raise ValueError(f"{name} must be a nonempty unique IPv4 CIDR list, at most 64 entries")
    networks = [ipaddress.ip_network(c, strict=True) for c in values]
    if any(n.version != 4 or n.prefixlen == 0 for n in networks):
        raise ValueError(f"{name} requires exact IPv4 ranges, not default routes")
    return networks

vpc_networks = ipv4_ranges("vpcCidrs", vpc_cidrs)
node_networks = ipv4_ranges("nodeCidrs", node_cidrs)
pod_networks = ipv4_ranges("podCidrs", pod_cidrs)
endpoint_networks = ipv4_ranges("privateEndpointCidrs", endpoint_cidrs)
ipv4_ranges("serviceCidr", [service_cidr])
if not isinstance(node_subnet_ids, list) or not node_subnet_ids or len(set(node_subnet_ids)) != len(node_subnet_ids) or not all(re.fullmatch(r"subnet-[0-9a-f]+", s) for s in node_subnet_ids):
    raise ValueError("Supply the exact selected managed-nodegroup subnet IDs")
if not isinstance(node_group_names, list) or not 1 <= len(node_group_names) <= 8 or len(set(node_group_names)) != len(node_group_names) or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", n) for n in node_group_names):
    raise ValueError("Supply one to eight exact selected managed EKS nodegroup names")
if not selector or not operator_labels or operator_namespace == namespace:
    raise ValueError("Explicit node pool and separate operator namespace/proxy labels required")
if not set(selector).difference({"kubernetes.io/os", "kubernetes.io/arch"}):
    raise ValueError("nodeSelector must identify the approved managed pool, not only OS/architecture")
expected_proxy_labels = {"tailscale.com/managed": "true", "tailscale.com/parent-resource": "dify",
    "tailscale.com/parent-resource-ns": namespace, "tailscale.com/parent-resource-type": "ingress"}
if any(operator_labels.get(key) != value for key, value in expected_proxy_labels.items()):
    raise ValueError("operatorProxyLabels must select only this Ingress's managed Tailscale proxy")
if restore_snapshot and set(pvc_snapshots) != {"redis-data", "weaviate-data", "plugin-data", "local-sandbox-home", "local-sandbox-workspace"}:
    raise ValueError("Restore requires the complete five-PVC snapshot set, not partial component recovery")
if not re.fullmatch(r"[0-9a-f]{64}", cfg.require("platformEvidenceSha256")):
    raise ValueError("Record the reviewed strict CNI/CSI/operator/RBAC platform evidence hash")
if not re.fullmatch(rf"oidc\.eks\.{region}\.amazonaws\.com/id/[A-Z0-9]+", issuer):
    raise ValueError("Exact regional EKS OIDC issuer required")
if not boundary.startswith(f"arn:aws:iam::{account}:policy/"):
    raise ValueError("IRSA permissions boundary must belong to selected account")
selector.update({"kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64"})
origin = f"https://{hostname}.{tailnet}"
ca = (ROOT / "config/rds-ca.pem").read_bytes()
if hashlib.sha256(ca).hexdigest() != pins["rds_ca_sha256"]:
    raise ValueError("RDS CA bytes differ from reviewed pin")
for image in pins["images"].values():
    if not re.fullmatch(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("Every application image must be digest-pinned")
config_revision = hashlib.sha256(b"".join(p.read_bytes() for p in
    sorted([ROOT / "pins.json", *(ROOT / "config").iterdir()]))).hexdigest()

def secret(key, *, rds=False):
    def validate(value):
        if len(value) < 32 or len(value) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError(f"{key} must contain 32..128 printable non-space characters")
        if rds and any(c in value for c in '/@"'):
            raise ValueError("RDS master password contains a forbidden character")
        if key in ("dbAppPassword", "dbPluginPassword") and not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
            raise ValueError("Database app passwords must be 32..128 URL-safe characters for native driver DSNs")
        if key == "agentServerSecretKey":
            return validate_agent_server_secret(value)
        return value
    return cfg.require_secret(key).apply(validate)

def replicas(key, default):
    value = cfg.get_int(key)
    value = default if value is None else value
    if not 1 <= value <= 20:
        raise ValueError(f"{key} must be 1..20; maintenance scaling is an attended operation")
    return value

api_replicas = replicas("apiReplicas", 2)
web_replicas = replicas("webReplicas", 2)
worker_replicas = replicas("workerReplicas", 1)
master_password = secret("dbMasterPassword", rds=True)
app_password = secret("dbAppPassword")
plugin_password = secret("dbPluginPassword")
redis_password = secret("redisPassword")
app_key = secret("secretKey")
plugin_key = secret("pluginKey")
inner_key = secret("pluginInnerKey")
sandbox_key = secret("sandboxKey")
weaviate_key = secret("weaviateKey")
agent_token = secret("agentApiToken")
agent_secret = secret("agentServerSecretKey")
shell_token = secret("agentSandboxToken")

# No implicit provider, account, kubeconfig context or static bearer token.
awsp = aws.Provider("aws", region=region, allowed_account_ids=[account],
    default_tags={"tags": {"pulumi-project": pulumi.get_project(), "pulumi-stack": stack}})
ao = pulumi.ResourceOptions(provider=awsp)
data_opts = pulumi.ResourceOptions(provider=awsp, protect=True)
cluster = aws.eks.get_cluster(name=cluster_name, opts=pulumi.InvokeOptions(provider=awsp))
if cluster.version != "1.35" or cluster.vpc_config.vpc_id != vpc or cluster.identities[0].oidcs[0].issuer != "https://" + issuer:
    raise ValueError("Selected EKS version/VPC/OIDC identity differs from platform contract")
if cluster.arn != f"arn:aws:eks:{region}:{account}:cluster/{cluster_name}":
    raise ValueError("Unexpected EKS account/region/name")
if cluster.vpc_config.endpoint_public_access is not False or cluster.vpc_config.endpoint_private_access is not True:
    raise ValueError("An existing private-only EKS endpoint and approved private runner route/DNS are required; this stack never changes shared endpoints")
vpc_metadata = aws.ec2.get_vpc(id=vpc, opts=pulumi.InvokeOptions(provider=awsp))
actual_vpc_cidrs = {vpc_metadata.cidr_block} | {a.cidr_block for a in vpc_metadata.cidr_block_associations if a.state == "associated"}
if vpc_metadata.owner_id != account or actual_vpc_cidrs != set(vpc_cidrs):
    raise ValueError("vpcCidrs must exactly match all associated IPv4 CIDRs of the selected account/VPC")
network_config = cluster.kubernetes_network_configs
if len(network_config) != 1 or network_config[0].ip_family != "ipv4" or network_config[0].service_ipv4_cidr != service_cidr:
    raise ValueError("Require the exact EKS IPv4 Service CIDR; dual-stack/unknown service ranges are not eligible")
if any(not any(n.subnet_of(v) for v in vpc_networks) for n in pod_networks + node_networks + endpoint_networks):
    raise ValueError("VPC CNI pod/node/private-endpoint ranges must belong to the verified VPC IPv4 ranges")
groups = [aws.eks.get_node_group(cluster_name=cluster_name, node_group_name=n,
    opts=pulumi.InvokeOptions(provider=awsp)) for n in node_group_names]
if set(node_subnet_ids) != {s for group in groups for s in group.subnet_ids}:
    raise ValueError("nodeSubnetIds must exactly match the selected managed nodegroups")
for group in groups:
    if group.status != "ACTIVE" or group.version != "1.35":
        raise ValueError("Selected managed nodegroups must be active EKS1.35 groups")
    for label, value in selector.items():
        actual_value = group.node_group_name if label == "eks.amazonaws.com/nodegroup" else group.labels.get(label)
        if label not in ("kubernetes.io/os", "kubernetes.io/arch") and value != actual_value:
            raise ValueError("Node selector must match every selected managed nodegroup; selector overlap/ENI inventory still requires platform proof")
node_subnets = [aws.ec2.get_subnet(id=s, opts=pulumi.InvokeOptions(provider=awsp)) for s in node_subnet_ids]
if any(s.vpc_id != vpc or s.map_public_ip_on_launch is not False or s.assign_ipv6_address_on_creation is not False or s.ipv6_native is not False
       for s in node_subnets) or {s.cidr_block for s in node_subnets} != set(node_cidrs):
    raise ValueError("Node CIDRs must match the selected VPC/subnets with public-IP assignment disabled; actual primary/secondary ENI/EIP/IPv6 inventory is additionally required")
control_subnets = [aws.ec2.get_subnet(id=s, opts=pulumi.InvokeOptions(provider=awsp)) for s in cluster.vpc_config.subnet_ids]
control_networks = [ipaddress.ip_network(s.cidr_block, strict=True) for s in control_subnets if s.vpc_id == vpc]
if len(control_networks) != len(control_subnets) or any(not any(n.subnet_of(s) for s in control_networks) for n in endpoint_networks):
    raise ValueError("privateEndpointCidrs must lie within verified EKS control-plane subnets; owner must reconcile actual private endpoint ENIs/DNS")
management_cidrs = [str(n) for n in ipaddress.collapse_addresses(
    vpc_networks + node_networks + pod_networks + endpoint_networks + [ipaddress.ip_network(service_cidr)])]
# A hash/metadata snapshot does not prove node-local isolation or absence of public ENI addresses.
# Execution additionally requires owner inventory/admission invariants and actual network-denial tests.
config_revision = hashlib.sha256((config_revision + json.dumps(management_cidrs)).encode()).hexdigest()
subnet_metadata = [aws.ec2.get_subnet(id=s, opts=pulumi.InvokeOptions(provider=awsp)) for s in subnets]
if len({s.availability_zone for s in subnet_metadata}) != 2 or any(s.vpc_id != vpc for s in subnet_metadata):
    raise ValueError("RDS subnets must belong to the selected VPC and two distinct AZs")
if {s.cidr_block for s in subnet_metadata} != set(cfg.require_object("rdsSubnetCidrs")):
    raise ValueError("RDS policy CIDRs must exactly match the selected subnet metadata")
kubeconfig = json.dumps({"apiVersion": "v1", "kind": "Config", "clusters": [{"name": cluster_name, "cluster": {
    "server": cluster.endpoint, "certificate-authority-data": cluster.certificate_authorities[0].data}}],
    "contexts": [{"name": "dify", "context": {"cluster": cluster_name, "user": "runner", "namespace": namespace}}],
    "current-context": "dify", "users": [{"name": "runner", "user": {"exec": {
        "apiVersion": "client.authentication.k8s.io/v1beta1", "command": "aws",
        "args": ["eks", "get-token", "--cluster-name", cluster_name, "--region", region],
        "interactiveMode": "Never"}}}]})
kp = k8s.Provider("eks", kubeconfig=kubeconfig, namespace=namespace, delete_unreachable=False,
                  skip_update_unreachable=False, upsert_existing_objects=False)
ko = pulumi.ResourceOptions(provider=kp)

bucket = aws.s3.BucketV2("files", force_destroy=False, opts=data_opts)
public_block = aws.s3.BucketPublicAccessBlock("files", bucket=bucket.id, block_public_acls=True,
    block_public_policy=True, ignore_public_acls=True, restrict_public_buckets=True, opts=ao)
aws.s3.BucketOwnershipControls("files", bucket=bucket.id, rule={"object_ownership": "BucketOwnerEnforced"}, opts=ao)
versioning = aws.s3.BucketVersioningV2("files", bucket=bucket.id, versioning_configuration={"status": "Enabled"}, opts=ao)
encryption = aws.s3.BucketServerSideEncryptionConfigurationV2("files", bucket=bucket.id,
    rules=[{"apply_server_side_encryption_by_default": {"sse_algorithm": "AES256"}}], opts=ao)
aws.s3.BucketPolicy("tls-only", bucket=bucket.id, policy=bucket.arn.apply(lambda arn: json.dumps({
    "Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Principal": "*", "Action": "s3:*",
    "Resource": [arn, arn + "/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]})), opts=ao)
role = aws.iam.Role("storage", name=f"dify-{stack}-storage", permissions_boundary=boundary,
    assume_role_policy=json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
    "Principal": {"Federated": f"arn:aws:iam::{account}:oidc-provider/{issuer}"},
    "Action": "sts:AssumeRoleWithWebIdentity", "Condition": {"StringEquals": {
        issuer + ":aud": "sts.amazonaws.com", issuer + ":sub": f"system:serviceaccount:{namespace}:dify-storage"}}}]}), opts=ao)
role_policy = aws.iam.RolePolicy("storage", role=role.id, policy=bucket.arn.apply(lambda arn: json.dumps({
    "Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"], "Resource": arn},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], "Resource": arn + "/*"}]})), opts=ao)
db_subnets = aws.rds.SubnetGroup("db", subnet_ids=subnets, opts=ao)
db_sg = aws.ec2.SecurityGroup("db", vpc_id=vpc, description="PostgreSQL from selected EKS node SG only",
    ingress=[{"protocol": "tcp", "from_port": 5432, "to_port": 5432, "security_groups": [node_sg]}],
    egress=[], opts=ao)
parameters = aws.rds.ParameterGroup("db", family="postgres15", parameters=[
    {"name": "rds.force_ssl", "value": "1", "apply_method": "pending-reboot"},
    {"name": "log_statement", "value": "none"}, {"name": "log_min_error_statement", "value": "panic"}], opts=ao)
db = aws.rds.Instance("db", identifier=f"dify-{stack}", engine="postgres", engine_version=pins["rds_engine"],
    instance_class="db.t4g.small", allocated_storage=20, storage_type="gp3", storage_encrypted=True,
    db_name=None if restore_snapshot else "postgres", username=None if restore_snapshot else "dify_admin",
    password=None if restore_snapshot else master_password, snapshot_identifier=restore_snapshot, port=5432,
    db_subnet_group_name=db_subnets.name, vpc_security_group_ids=[db_sg.id], parameter_group_name=parameters.name,
    publicly_accessible=False, multi_az=False, backup_retention_period=7, auto_minor_version_upgrade=False,
    deletion_protection=True, skip_final_snapshot=False, final_snapshot_identifier=f"dify-{stack}-final",
    copy_tags_to_snapshot=True, delete_automated_backups=False, opts=data_opts)

def meta(name):
    return {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/part-of": "dify", "app": name}}

# No namespace, RBAC, CNI, CSI or operator resources are adopted/created here.
storage_sa = k8s.core.v1.ServiceAccount("storage", metadata={**meta("dify-storage"),
    "annotations": {"eks.amazonaws.com/role-arn": role.arn}}, automount_service_account_token=False, opts=ko)
plain_sa = k8s.core.v1.ServiceAccount("no-identity", metadata=meta("dify-no-identity"), automount_service_account_token=False, opts=ko)
deny = k8s.networking.v1.NetworkPolicy("default-deny", metadata=meta("default-deny"),
    spec={"pod_selector": {}, "policy_types": ["Ingress", "Egress"], "ingress": [], "egress": []}, opts=ko)
policies = [deny]
def peer(app):
    return {"pod_selector": {"match_labels": {"app": app}}}

def ports(numbers):
    return [{"protocol": "TCP", "port": n} for n in numbers]

def allow(name, app, ingress=(), egress=()):
    policy = k8s.networking.v1.NetworkPolicy(name, metadata=meta(name), spec={
        "pod_selector": {"match_labels": {"app": app}}, "policy_types": ["Ingress", "Egress"],
        "ingress": list(ingress), "egress": list(egress)}, opts=pulumi.ResourceOptions(provider=kp, depends_on=[deny]))
    policies.append(policy)

dns = {"to": [{"namespace_selector": {"match_labels": {"kubernetes.io/metadata.name": "kube-system"}},
               "pod_selector": {"match_labels": {"k8s-app": "kube-dns"}}}],
       "ports": [{"protocol": p, "port": 53} for p in ("TCP", "UDP")]}
private_blocks = [str(n) for n in ipaddress.collapse_addresses(ipaddress.ip_network(c) for c in
    ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
     "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4"] + management_cidrs)]
# IP-based public HTTPS egress, NOT an S3/STS FQDN allowlist. Explicit consumers only.
public_https = {"to": [{"ip_block": {"cidr": "0.0.0.0/0", "except_": private_blocks}}], "ports": ports([443])}
public_web = {**public_https, "ports": ports([80, 443])}
rds_path = {"to": [{"ip_block": {"cidr": c}} for c in cfg.require_object("rdsSubnetCidrs")], "ports": ports([5432])}
# RDS subnet CIDRs are independently checked by platform owner against supplied subnet IDs.
for c in cfg.require_object("rdsSubnetCidrs"):
    if not ipaddress.ip_network(c, strict=True).is_private:
        raise ValueError("RDS paths require exact private subnet CIDRs")
def to(app, numbers):
    return {"to": [peer(app)], "ports": ports(numbers)}
def incoming(apps, numbers):
    return {"from_": [peer(a) for a in apps], "ports": ports(numbers)}

consumers = ["api", "worker", "beat", "migration"]
for app in consumers:
    allow("paths-" + app, app,
        ingress=[incoming(["nginx", "plugin", "agent", "agent-proxy"], [5001])] if app == "api" else [],
        egress=[dns, rds_path, public_https, to("redis", [6379]), to("weaviate", [8080, 50051]),
                to("plugin", [5002]), to("sandbox", [8194]), to("agent", [5050]), to("proxy", [3128])])
allow("paths-db-init", "db-init", egress=[dns, rds_path])
allow("paths-redis", "redis", ingress=[incoming(consumers + ["plugin", "agent"], [6379])], egress=[dns])
allow("paths-weaviate", "weaviate", ingress=[incoming(consumers, [8080, 50051])], egress=[dns])
allow("paths-web", "web", ingress=[incoming(["nginx"], [3000])], egress=[dns, to("api", [5001])])
allow("paths-plugin", "plugin", ingress=[incoming(consumers + ["nginx", "agent"], [5002])],
    egress=[dns, rds_path, to("redis", [6379]), to("api", [5001]), to("proxy", [3128]), public_https])
allow("paths-sandbox", "sandbox", ingress=[incoming(consumers + ["proxy"], [8194])], egress=[dns, to("proxy", [3128])])
allow("paths-agent", "agent", ingress=[incoming(consumers + ["nginx", "local-sandbox", "agent-proxy"], [5050])],
    egress=[dns, to("redis", [6379]), to("plugin", [5002]), to("api", [5001]), to("local-sandbox", [5004]), to("proxy", [3128]), public_https])
allow("paths-local-sandbox", "local-sandbox", ingress=[incoming(["agent"], [5004])],
    egress=[dns, to("agent-proxy", [3128]), to("agent", [5050])])
allow("paths-proxy", "proxy", ingress=[incoming(consumers + ["plugin", "agent", "sandbox"], [3128])],
    egress=[dns, public_web, to("sandbox", [8194])])
allow("paths-agent-proxy", "agent-proxy", ingress=[incoming(["local-sandbox"], [3128])],
    egress=[dns, public_web, to("agent", [5050]), to("api", [5001])])
allow("paths-nginx", "nginx", ingress=[{"from_": [{"namespace_selector": {"match_labels": {
    "kubernetes.io/metadata.name": operator_namespace}}, "pod_selector": {"match_labels": operator_labels}}], "ports": ports([8080])}],
    egress=[dns, to("api", [5001]), to("web", [3000]), to("plugin", [5002]), to("agent", [5050])])

ca_map = k8s.core.v1.ConfigMap("ca", metadata=meta("rds-ca"), data={"rds-ca.pem": ca.decode()}, opts=ko)
init_map = k8s.core.v1.ConfigMap("db-init", metadata=meta("db-init"),
    data={"init.sql": (ROOT / "config/db-init.sql").read_text()}, opts=ko)
config_maps = {}
for name, files in {"nginx": ["nginx.conf"], "proxy": ["squid-common.conf", "squid.conf"],
                    "agent-proxy": ["squid-common.conf", "squid-agent.conf"]}.items():
    data = {}
    for file in files:
        text = (ROOT / "config" / file).read_text()
        if file == "squid-common.conf":
            text += "\nacl private dst " + " ".join(management_cidrs) + "\n"
        data["common.conf" if file == "squid-common.conf" else "squid.conf" if file == "squid-agent.conf" else file] = text
    config_maps[name] = k8s.core.v1.ConfigMap(name, metadata=meta(name + "-config"), data=data, opts=ko)

def env(values):
    return [{"name": name, "value": value} for name, value in values.items()]
def secret_env(name, key):
    return {"name": key, "value_from": {"secret_key_ref": {"name": name, "key": key}}}

# Separate secrets keep the master password and unrelated service keys out of app pods.
app_values = {"SECRET_KEY": app_key, "DB_PASSWORD": app_password, "REDIS_PASSWORD": redis_password,
    "CELERY_BROKER_URL": redis_password.apply(lambda p: "redis://:" + quote(p, safe="") + "@redis:6379/1"),
    "PLUGIN_DAEMON_KEY": plugin_key, "INNER_API_KEY_FOR_PLUGIN": inner_key,
    "CODE_EXECUTION_API_KEY": sandbox_key, "WEAVIATE_API_KEY": weaviate_key, "AGENT_BACKEND_API_TOKEN": agent_token}
app_secrets = k8s.core.v1.Secret("app", metadata=meta("app-secrets"), string_data=app_values, opts=ko)
plugin_values = {"DB_PASSWORD": plugin_password, "REDIS_PASSWORD": redis_password,
    "SERVER_KEY": plugin_key, "DIFY_INNER_API_KEY": inner_key}
plugin_secrets = k8s.core.v1.Secret("plugin", metadata=meta("plugin-secrets"), string_data=plugin_values, opts=ko)
agent_values = {"DIFY_AGENT_API_TOKEN": agent_token, "DIFY_AGENT_SERVER_SECRET_KEY": agent_secret,
    "DIFY_AGENT_REDIS_URL": redis_password.apply(lambda p: "redis://:" + quote(p, safe="") + "@redis:6379/2"),
    "DIFY_AGENT_PLUGIN_DAEMON_API_KEY": plugin_key, "DIFY_AGENT_INNER_API_KEY": inner_key,
    "DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN": shell_token}
agent_secrets = k8s.core.v1.Secret("agent", metadata=meta("agent-secrets"), string_data=agent_values, opts=ko)
init_values = {"PGPASSWORD": master_password, "APP_PASSWORD": app_password, "PLUGIN_PASSWORD": plugin_password}
init_secrets = k8s.core.v1.Secret("db-init", metadata=meta("db-init-secrets"), string_data=init_values, opts=ko)
redis_secrets = k8s.core.v1.Secret("redis", metadata=meta("redis-secrets"), string_data={"REDIS_PASSWORD": redis_password}, opts=ko)
weaviate_secrets = k8s.core.v1.Secret("weaviate", metadata=meta("weaviate-secrets"), string_data={"AUTHENTICATION_APIKEY_ALLOWED_KEYS": weaviate_key}, opts=ko)
shell_secrets = k8s.core.v1.Secret("shell", metadata=meta("shell-secrets"), string_data={"SHELLCTL_AUTH_TOKEN": shell_token}, opts=ko)
sandbox_secrets = k8s.core.v1.Secret("sandbox", metadata=meta("sandbox-secrets"), string_data={"API_KEY": sandbox_key,
    "config.yaml": sandbox_key.apply(lambda key: json.dumps({"app": {"port": 8194, "debug": False, "key": key},
    "max_workers": 4, "max_requests": 50, "worker_timeout": 15, "enable_network": True,
    "allowed_syscalls": [], "proxy": {"http": "http://proxy:3128", "https": "http://proxy:3128", "socks5": ""}}))}, opts=ko)

common = {"DEPLOY_ENV": "PRODUCTION", "DEPLOYMENT_EDITION": "COMMUNITY", "MIGRATION_ENABLED": "false",
    "CONSOLE_WEB_URL": origin, "CONSOLE_API_URL": origin, "SERVICE_API_URL": origin,
    "APP_WEB_URL": origin, "APP_API_URL": origin, "FILES_URL": origin,
    "INTERNAL_FILES_URL": "http://api:5001", "TRIGGER_URL": origin,
    "ENDPOINT_URL_TEMPLATE": origin + "/e/{hook_id}", "CONSOLE_CORS_ALLOW_ORIGINS": origin,
    "WEB_API_CORS_ALLOW_ORIGINS": origin, "DB_TYPE": "postgresql", "DB_HOST": db.address,
    "DB_PORT": "5432", "DB_USERNAME": "dify_owner", "DB_DATABASE": "dify",
    "DB_EXTRAS": "sslmode=verify-full&sslrootcert=/etc/rds/rds-ca.pem", "REDIS_HOST": "redis",
    "REDIS_PORT": "6379", "REDIS_DB": "0", "REDIS_KEEPALIVE": "true",
    "STORAGE_TYPE": "s3", "KEY_PROVIDER_TYPE": "local", "S3_BUCKET_NAME": bucket.id,
    "S3_REGION": region, "S3_USE_AWS_MANAGED_IAM": "true", "AWS_REGION": region,
    "AWS_DEFAULT_REGION": region, "AWS_STS_REGIONAL_ENDPOINTS": "regional", "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_ROLE_ARN": role.arn, "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/dify-identity/token",
    "VECTOR_STORE": "weaviate", "WEAVIATE_ENDPOINT": "http://weaviate:8080",
    "WEAVIATE_GRPC_ENDPOINT": "grpc://weaviate:50051", "PLUGIN_DAEMON_URL": "http://plugin:5002",
    "PLUGIN_MAX_PACKAGE_SIZE": "52428800", "AGENT_BACKEND_BASE_URL": "http://agent:5050",
    "NEXT_PUBLIC_ENABLE_AGENT_V2": "true", "CODE_EXECUTION_ENDPOINT": "http://sandbox:8194",
    "SSRF_PROXY_HTTP_URL": "http://proxy:3128", "SSRF_PROXY_HTTPS_URL": "http://proxy:3128",
    "LOG_FILE": "", "SERVER_WORKER_CLASS": "gevent", "GUNICORN_TIMEOUT": "360", "CELERY_WORKER_AMOUNT": "1"}
ca_volume = {"name": "rds-ca", "config_map": {"name": "rds-ca"}}
ca_mount = {"name": "rds-ca", "mount_path": "/etc/rds", "read_only": True}
token_volume = {"name": "aws-identity", "projected": {"sources": [{"service_account_token": {
    "audience": "sts.amazonaws.com", "expiration_seconds": 3600, "path": "token"}}]}}
token_mount = {"name": "aws-identity", "mount_path": "/var/run/dify-identity", "read_only": True}

def pod(name, image, values, secret_name=None, secret_keys=(), *, volumes=(), mounts=(), identity=False,
        cpu="100m", memory="256Mi", memory_limit="1Gi", grace=60, probe=None, args=None, command=None):
    container = {"name": name, "image": pins["images"][image], "image_pull_policy": "IfNotPresent",
        "env": env(values) + [secret_env(secret_name, key) for key in secret_keys],
        "volume_mounts": list(mounts), "resources": {"requests": {"cpu": cpu, "memory": memory},
        "limits": {"cpu": "2", "memory": memory_limit}},
        "security_context": {"privileged": False, "seccomp_profile": {"type": "RuntimeDefault"}}}
    if args is not None:
        container["args"] = args
    if command is not None:
        container["command"] = command
    if probe:
        container["readiness_probe"] = {"http_get": {"path": probe[1], "port": probe[0]}, "period_seconds": 10, "timeout_seconds": 5}
        container["startup_probe"] = {"http_get": {"path": probe[1], "port": probe[0]}, "period_seconds": 10, "failure_threshold": 60, "timeout_seconds": 5}
    elif name in ("plugin", "agent", "proxy", "agent-proxy"):
        port = {"plugin": 5002, "agent": 5050, "proxy": 3128, "agent-proxy": 3128}[name]
        container["readiness_probe"] = {"tcp_socket": {"port": port}, "period_seconds": 10, "timeout_seconds": 5}
        container["startup_probe"] = {"tcp_socket": {"port": port}, "period_seconds": 10, "failure_threshold": 60}
    return {"node_selector": selector, "service_account_name": "dify-storage" if identity else "dify-no-identity",
        "automount_service_account_token": False, "termination_grace_period_seconds": grace,
        "enable_service_links": False, "containers": [container], "volumes": list(volumes),
        "security_context": {"seccomp_profile": {"type": "RuntimeDefault"}}}

base_dependencies = policies + [storage_sa, plain_sa, role_policy, public_block, versioning, encryption, ca_map]
services = {}
for name, numbers in {"api": [5001], "web": [3000], "redis": [6379], "weaviate": [8080, 50051],
                      "plugin": [5002], "sandbox": [8194], "agent": [5050], "local-sandbox": [5004],
                      "proxy": [3128], "agent-proxy": [3128], "nginx": [8080]}.items():
    services[name] = k8s.core.v1.Service(name, metadata=meta(name), spec={"type": "ClusterIP", "selector": {"app": name},
        "ports": [{"name": "tcp-" + str(p), "port": p, "target_port": p} for p in numbers]}, opts=ko)

def deployment(name, spec, count=1, deps=(), recreate=False):
    if name in ("api", "web", "worker"):
        spec["affinity"] = {"pod_anti_affinity": {"preferred_during_scheduling_ignored_during_execution": [{"weight": 100,
            "pod_affinity_term": {"label_selector": {"match_labels": {"app": name}}, "topology_key": "kubernetes.io/hostname"}}]}}
        spec["topology_spread_constraints"] = [{"max_skew": 1, "min_domains": 2,
            "topology_key": "kubernetes.io/hostname", "when_unsatisfiable": "DoNotSchedule",
            "label_selector": {"match_labels": {"app": name}}}]
    return k8s.apps.v1.Deployment(name, metadata=meta(name), spec={"replicas": count, "progress_deadline_seconds": 900,
        "selector": {"match_labels": {"app": name}}, "strategy": {"type": "Recreate"} if recreate else {
            "type": "RollingUpdate", "rolling_update": {"max_unavailable": 0, "max_surge": 1}},
        "template": {"metadata": {"labels": meta(name)["labels"], "annotations": {"dify.example/config-sha256": config_revision}}, "spec": spec}},
        opts=pulumi.ResourceOptions(provider=kp, depends_on=base_dependencies + list(deps), custom_timeouts=pulumi.CustomTimeouts(create="20m", update="20m")))

def stateful(name, spec, claims, deps=()):
    pvcs = []
    for suffix, path, size in claims:
        claim = name + "-" + suffix
        claim_spec = {
            "access_modes": ["ReadWriteOnce"], "storage_class_name": storage_class,
            "resources": {"requests": {"storage": str(size) + "Gi"}}}
        if claim in pvc_snapshots:
            claim_spec["data_source"] = {"api_group": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": pvc_snapshots[claim]}
        pvcs.append(k8s.core.v1.PersistentVolumeClaim(claim, metadata=meta(claim), spec=claim_spec,
            opts=pulumi.ResourceOptions(provider=kp, protect=True)))
        spec["volumes"].append({"name": suffix, "persistent_volume_claim": {"claim_name": claim}})
        spec["containers"][0]["volume_mounts"].append({"name": suffix, "mount_path": path})
    # Kubernetes fails closed if any named PVC cannot mount; no hostPath/emptyDir fallback.
    return k8s.apps.v1.StatefulSet(name, metadata=meta(name), spec={"replicas": 1, "service_name": name,
        "selector": {"match_labels": {"app": name}}, "template": {"metadata": {"labels": meta(name)["labels"],
        "annotations": {"dify.example/config-sha256": config_revision}}, "spec": spec}},
        opts=pulumi.ResourceOptions(provider=kp, depends_on=base_dependencies + list(deps) + pvcs,
                                    custom_timeouts=pulumi.CustomTimeouts(create="20m", update="20m")))

redis_pod = pod("redis", "redis", {}, "redis-secrets", ["REDIS_PASSWORD"],
    command=["sh", "-ec"], args=['exec docker-entrypoint.sh redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"'])
redis_pod["containers"][0]["readiness_probe"] = {"exec_": {"command": ["sh", "-ec", 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping | grep -qx PONG']}, "timeout_seconds": 5}
redis = stateful("redis", redis_pod, [("data", "/data", 5)], [redis_secrets])
weaviate = stateful("weaviate", pod("weaviate", "weaviate", {
    "PERSISTENCE_DATA_PATH": "/var/lib/weaviate", "QUERY_DEFAULTS_LIMIT": "25", "DEFAULT_VECTORIZER_MODULE": "none",
    "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED": "false", "AUTHENTICATION_APIKEY_ENABLED": "true",
    "AUTHENTICATION_APIKEY_USERS": "dify", "AUTHORIZATION_ADMINLIST_ENABLED": "true", "AUTHORIZATION_ADMINLIST_USERS": "dify",
    "CLUSTER_HOSTNAME": "node1"}, "weaviate-secrets", ["AUTHENTICATION_APIKEY_ALLOWED_KEYS"],
    cpu="500m", memory="1Gi", memory_limit="3Gi", probe=(8080, "/v1/.well-known/ready")), [("data", "/var/lib/weaviate", 20)], [weaviate_secrets])
for name in ("proxy", "agent-proxy"):
    deployment(name, pod(name, "proxy", {}, volumes=[{"name": "config", "config_map": {"name": name + "-config"}}],
        mounts=[{"name": "config", "mount_path": "/etc/squid", "read_only": True}]), deps=[config_maps[name]])
sandbox = deployment("sandbox", pod("sandbox", "sandbox", {"ENABLE_NETWORK": "true", "GIN_MODE": "release", "WORKER_TIMEOUT": "15",
    "HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3128"}, "sandbox-secrets", ["API_KEY"],
    volumes=[{"name": "config", "secret": {"secret_name": "sandbox-secrets", "items": [{"key": "config.yaml", "path": "config.yaml"}]}}],
    mounts=[{"name": "config", "mount_path": "/conf", "read_only": True}], probe=(8194, "/health")), deps=[sandbox_secrets])
local_sandbox_pod = pod("local-sandbox", "local-sandbox", {
    "SHELLCTL_ENABLE_PATH_ISOLATION": "true", "HTTP_PROXY": "http://agent-proxy:3128", "HTTPS_PROXY": "http://agent-proxy:3128",
    "NO_PROXY": "localhost,127.0.0.1"}, "shell-secrets", ["SHELLCTL_AUTH_TOKEN"],
    args=["shellctl", "serve", "--listen", "0.0.0.0:5004", "--require-auth-token"], probe=(5004, "/healthz"))
# Exact pinned AMD64 image: USER dify, /etc/passwd + /etc/group UID/GID1000.
# Apply only to these two CSI volumes; preserve the image's nonroot startup user.
if pins["agent_runtime_identity"]["uid"] != 1000 or pins["agent_runtime_identity"]["gid"] != 1000:
    raise ValueError("Agent identity pin differs from inspected immutable image; re-review ownership before changing it")
local_sandbox_pod["security_context"].update({"fs_group": pins["agent_runtime_identity"]["gid"], "fs_group_change_policy": "OnRootMismatch"})
# Mounting a fresh home also hides the image's three shell skeleton files.
# Seed only an otherwise empty home, never overwrite an existing/restored home.
local_sandbox_pod["init_containers"] = [{"name": "seed-agent-home", "image": pins["images"]["local-sandbox"],
    "image_pull_policy": "IfNotPresent", "command": ["/bin/sh", "-ec"], "args": [
        'test -w /mnt/dify-home; test -r /mnt/dify-home; test -x /mnt/dify-home; test -w /mnt/dify-workspace; '
        'home_entries="$(find /mnt/dify-home -mindepth 1 -maxdepth 1 ! -name lost+found -print -quit)"; '
        'if [ -z "$home_entries" ]; then '
        'cp -p /home/dify/.bash_logout /home/dify/.bashrc /home/dify/.profile /mnt/dify-home/; fi'],
    "volume_mounts": [{"name": "home", "mount_path": "/mnt/dify-home"}, {"name": "workspace", "mount_path": "/mnt/dify-workspace"}],
    "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
    "security_context": {"privileged": False, "seccomp_profile": {"type": "RuntimeDefault"}}}]
local_sandbox = stateful("local-sandbox", local_sandbox_pod,
    [("home", "/home/dify", 5), ("workspace", "/workspace", 10)], [shell_secrets])

def job(name, spec, deps):
    spec["restart_policy"] = "Never"
    labels = meta("migration" if name.startswith("migration-") else name)["labels"]
    return k8s.batch.v1.Job(name, metadata=meta(name), spec={"backoff_limit": 0, "active_deadline_seconds": 600,
        "template": {"metadata": {"labels": labels}, "spec": spec}},
        opts=pulumi.ResourceOptions(provider=kp, depends_on=base_dependencies + deps, delete_before_replace=True,
            custom_timeouts=pulumi.CustomTimeouts(create="12m")))

init = job("db-init", pod("db-init", "db-init", {"PGHOST": db.address, "PGPORT": "5432", "PGUSER": "dify_admin",
    "PGDATABASE": "postgres", "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/etc/rds/rds-ca.pem", "PGCONNECT_TIMEOUT": "10"},
    "db-init-secrets", init_values.keys(), volumes=[ca_volume, {"name": "sql", "config_map": {"name": "db-init"}}],
    mounts=[ca_mount, {"name": "sql", "mount_path": "/sql", "read_only": True}], command=["psql"],
    args=["-X", "--quiet", "--no-psqlrc", "-f", "/sql/init.sql"]), [db, init_map, init_secrets])
migration_name = "migration-" + pins["dify_commit"][:12]
migration_pod = pod("migration", "api", {**common, "MODE": "migration", "MIGRATION_ENABLED": "true"},
    "app-secrets", app_values.keys(), volumes=[ca_volume, token_volume], mounts=[ca_mount, token_mount], identity=True,
    cpu="500m", memory="1Gi", memory_limit="2Gi")
migration = job(migration_name, migration_pod, [init, app_secrets, redis, weaviate])
# Native Job completion is awaited by the provider before consumers start.

plugin_pod = pod("plugin", "plugin", {"DB_TYPE": "postgresql", "DB_HOST": db.address,
    "DB_PORT": "5432", "DB_USERNAME": "dify_plugin_owner", "DB_DATABASE": "dify_plugin", "DB_SSL_MODE": "require",
    # pgx5.9.2's system selector promotes effective mode to verify-full before dispatch.
    # Keep the tuple indivisible; actual daemon/CLI modules and public root inventory still gate execution.
    "PGSSLROOTCERT": "system", "SSL_CERT_FILE": "/etc/rds/rds-ca.pem", "REDIS_HOST": "redis", "REDIS_PORT": "6379", "REDIS_DB": "0",
    "SERVER_PORT": "5002", "DIFY_INNER_API_URL": "http://api:5001", "FORCE_VERIFYING_SIGNATURE": "true",
    "PLUGIN_STORAGE_TYPE": "local", "PLUGIN_STORAGE_LOCAL_ROOT": "/app/storage", "PLUGIN_WORKING_PATH": "/app/storage/cwd",
    "PLUGIN_INSTALLED_PATH": "plugin", "PLUGIN_MEDIA_CACHE_PATH": "assets", "PLUGIN_PACKAGE_CACHE_PATH": "plugin_packages",
    "PERSISTENCE_STORAGE_PATH": "/app/storage/persistence", "DB_DEFAULT_DATABASE": "postgres", "DB_GORM_LOG_LEVEL": "silent",
    "PLATFORM": "local", "SERVER_HOST": "0.0.0.0", "SERVER_TZ": "UTC", "ROUTINE_POOL_SIZE": "10000",
    "PLUGIN_LOCAL_LAUNCHING_CONCURRENT": "2", "LIFETIME_COLLECTION_HEARTBEAT_INTERVAL": "5",
    "LIFETIME_COLLECTION_GC_INTERVAL": "60", "LIFETIME_STATE_GC_INTERVAL": "300",
    "DIFY_INVOCATION_CONNECTION_IDLE_TIMEOUT": "120", "MAX_BUNDLE_PACKAGE_SIZE": "629145600",
    "PIP_MIRROR_AUTO_DETECT": "false", "PIP_MIRROR_URL": "https://pypi.org/simple",
    "PYTHON_ENV_INIT_TIMEOUT": "120", "PLUGIN_MAX_EXECUTION_TIMEOUT": "600", "MAX_PLUGIN_PACKAGE_SIZE": "52428800",
    "PPROF_ENABLED": "false", "PLUGIN_REMOTE_INSTALLING_HOST": "127.0.0.1", "PLUGIN_REMOTE_INSTALLING_PORT": "5003",
    "HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3128", "NO_PROXY": "api,redis,localhost,127.0.0.1"},
    "plugin-secrets", plugin_values.keys(), volumes=[ca_volume], mounts=[ca_mount], cpu="250m", memory="512Mi", memory_limit="2Gi")
# The same fresh-PVC ownership boundary applies here, but Ubuntu's dify UID/GID is1001, not Agent's1000.
if pins["plugin_runtime_identity"]["uid"] != 1001 or pins["plugin_runtime_identity"]["gid"] != 1001:
    raise ValueError("Plugin identity pin differs from inspected immutable image; re-review volume ownership")
plugin_pod["security_context"].update({"fs_group": pins["plugin_runtime_identity"]["gid"], "fs_group_change_policy": "OnRootMismatch"})
plugin = stateful("plugin", plugin_pod,
    [("data", "/app/storage", 10)], [migration, plugin_secrets])
agent = stateful("agent", pod("agent", "agent", {"DIFY_AGENT_PLUGIN_DAEMON_URL": "http://plugin:5002",
    "DIFY_AGENT_INNER_API_URL": "http://api:5001", "DIFY_AGENT_RUNTIME_BACKEND": "local",
    "DIFY_AGENT_LOCAL_SANDBOX_ENDPOINT": "http://local-sandbox:5004", "DIFY_AGENT_SANDBOX_FILES_BASE_URL": "http://api:5001",
    "DIFY_AGENT_STUB_API_BASE_URL": "http://agent:5050/agent-stub", "HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3128",
    "NO_PROXY": "api,plugin,redis,local-sandbox,agent,localhost,127.0.0.1"}, "agent-secrets", agent_values.keys(),
    memory="512Mi", memory_limit="2Gi"), [], [plugin, local_sandbox, agent_secrets])
for name, mode, count in (("api", "api", api_replicas), ("worker", "worker", worker_replicas), ("beat", "beat", 1)):
    deployment(name, pod(name, "api", {**common, "MODE": mode}, "app-secrets", app_values.keys(),
        identity=True, volumes=[ca_volume, token_volume], mounts=[ca_mount, token_mount],
        cpu="500m" if name != "beat" else "100m", memory="1Gi" if name != "beat" else "512Mi", memory_limit="3Gi",
        grace=600 if name == "worker" else 60, probe=(5001, "/health") if name == "api" else None), count,
        [migration, plugin, agent, sandbox], recreate=name == "beat")
deployment("web", pod("web", "web", {"CONSOLE_API_URL": origin, "APP_API_URL": origin,
    "NEXT_PUBLIC_ENABLE_AGENT_V2": "true", "HOSTNAME": "0.0.0.0", "PORT": "3000"}, probe=(3000, "/")), web_replicas, [migration])
deployment("nginx", pod("nginx", "nginx", {}, volumes=[{"name": "config", "config_map": {"name": "nginx-config"}}],
    mounts=[{"name": "config", "mount_path": "/etc/nginx/nginx.conf", "sub_path": "nginx.conf", "read_only": True}], probe=(8080, "/healthz")),
    deps=[config_maps["nginx"]] + list(services.values()))
ingress = k8s.networking.v1.Ingress("private", metadata={**meta("dify"), "annotations": {"tailscale.com/tags": "tag:dify"}},
    spec={"ingress_class_name": "tailscale", "tls": [{"hosts": [hostname]}], "rules": [{"http": {"paths": [{
        "path": "/", "path_type": "Prefix", "backend": {"service": {"name": "nginx", "port": {"number": 8080}}}}]}}]},
    opts=pulumi.ResourceOptions(provider=kp, depends_on=policies + [services["nginx"]]))
pulumi.export("url", origin)
pulumi.export("namespace", namespace)
pulumi.export("bucketName", bucket.id)
pulumi.export("databaseIdentifier", db.identifier)
pulumi.export("migrationJob", migration_name)
