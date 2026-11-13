"""Relational authority and immutable snapshots. All owned references include workspace."""
from sqlalchemy import (MetaData, Table, Column as C, String as S, Text, Integer as I,
                        BigInteger as B, Boolean, Float, JSON, ForeignKeyConstraint as FK,
                        UniqueConstraint as UQ, CheckConstraint as CK)

metadata = MetaData(naming_convention={"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
                                     "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
                                     "pk": "pk_%(table_name)s"})

def owned(name, *cols):
    return Table(name, metadata, C("workspace_id", S(120), primary_key=True), C("id", S(120), primary_key=True),
                 FK(["workspace_id"], ["workspaces.id"]), *cols)


def ref(field, table):
    return FK(["workspace_id", field], [table + ".workspace_id", table + ".id"])

workspaces = Table("workspaces", metadata, C("id", S(120), primary_key=True), C("creator_id", S(200), nullable=False),
                   C("provision_key", S(120), nullable=False), C("generation", S(120), nullable=False),
                   C("created_at", Float, nullable=False), C("expires_at", Float, nullable=False),
                   C("revision", I, nullable=False), C("active_policy_id", S(120)),
                   UQ("creator_id", "provision_key"))
memberships = Table("memberships", metadata, C("workspace_id", S(120), primary_key=True),
                    C("actor_id", S(200), primary_key=True), C("role", S(30), nullable=False), C("active", Boolean, nullable=False),
                    FK(["workspace_id"], ["workspaces.id"]))
credentials = owned("agent_credentials", C("secret_hash", S(64), unique=True, nullable=False),
                    C("principal_id", S(200), nullable=False), C("mandate_id", S(120), nullable=False),
                    C("created_at", Float, nullable=False), C("expires_at", Float, nullable=False),
                    C("revoked", Boolean, nullable=False), ref("mandate_id", "mandates"))
policies = owned("policy_versions", C("content_hash", S(64), nullable=False), C("payload", JSON, nullable=False),
                 C("created_at", Float, nullable=False), C("admission", S(30), nullable=False), UQ("workspace_id", "content_hash"))
invoices = owned("invoices", C("namespace", S(120), nullable=False), C("source_id", S(120), nullable=False),
                 C("vendor_id", S(120), nullable=False), C("amount_minor", B, nullable=False),
                 C("revision", I, nullable=False), C("evidence_revision", I, nullable=False), C("payload", JSON, nullable=False),
                 UQ("workspace_id", "namespace", "source_id"), CK("amount_minor > 0", name="positive_amount"), ref("vendor_id", "vendors"))
vendors = owned("vendors", C("revision", I, nullable=False), C("payload", JSON, nullable=False))
evidence = owned("evidence_records", C("invoice_id", S(120), nullable=False), C("revision", I, nullable=False),
                 C("source_type", S(30), nullable=False), C("supersedes_id", S(120)), C("actor_id", S(200), nullable=False),
                 C("body_hash", S(64), nullable=False), C("payload", JSON, nullable=False),
                 ref("invoice_id", "invoices"), ref("supersedes_id", "evidence_records"), UQ("workspace_id", "invoice_id", "revision"))
mandates = owned("mandates", C("revision", I, nullable=False), C("generation", S(120), nullable=False),
                 C("policy_id", S(120), nullable=False), C("created_by", S(200), nullable=False),
                 C("created_at", Float, nullable=False), C("expires_at", Float, nullable=False), C("revoked", Boolean, nullable=False),
                 C("budget_minor", B, nullable=False), C("auto_limit_minor", B, nullable=False),
                 C("spent_minor", B, nullable=False), C("reserved_minor", B, nullable=False), C("payload", JSON, nullable=False),
                 ref("policy_id", "policy_versions"), CK("budget_minor > 0 AND auto_limit_minor >= 0 AND auto_limit_minor <= budget_minor", name="valid_limits"),
                 CK("spent_minor >= 0 AND reserved_minor >= 0 AND spent_minor + reserved_minor <= budget_minor", name="bounded_money"))
proposals = owned("proposals", C("mandate_id", S(120), nullable=False), C("request_id", S(120), nullable=False),
                  C("principal_id", S(200), nullable=False), C("credential_id", S(120)),
                  C("payload_hash", S(64), nullable=False), C("action_hash", S(64), nullable=False), C("payload", JSON, nullable=False),
                  C("state", S(30), nullable=False), C("verdict", S(10)), C("revision", I, nullable=False),
                  C("input_id", S(120)), C("rechecks", I, nullable=False), C("checks", JSON, nullable=False),
                  C("created_at", Float, nullable=False), C("updated_at", Float, nullable=False),
                  ref("mandate_id", "mandates"), ref("credential_id", "agent_credentials"),
                  UQ("workspace_id", "mandate_id", "request_id"))
inputs = owned("decision_inputs", C("proposal_id", S(120), nullable=False), C("input_hash", S(64), nullable=False),
               C("payload", JSON, nullable=False), C("created_at", Float, nullable=False), ref("proposal_id", "proposals"))
evaluations = owned("semantic_evaluations", C("input_id", S(120), nullable=False), C("request_hash", S(64), nullable=False),
                    C("backend", S(30), nullable=False), C("availability", S(30), nullable=False), C("stale", Boolean, nullable=False),
                    C("payload", JSON, nullable=False), C("created_at", Float, nullable=False), ref("input_id", "decision_inputs"))
reservations = owned("reservations", C("proposal_id", S(120), nullable=False), C("mandate_id", S(120), nullable=False),
                     C("amount_minor", B, nullable=False), C("state", S(30), nullable=False), C("release_reason", S(100)),
                     ref("proposal_id", "proposals"), ref("mandate_id", "mandates"), UQ("workspace_id", "proposal_id"),
                     CK("amount_minor > 0", name="positive_amount"))
claims = Table("payment_claims", metadata, C("workspace_id", S(120), primary_key=True), C("namespace", S(120), primary_key=True),
               C("invoice_id", S(120), primary_key=True), C("proposal_id", S(120)), C("state", S(30), nullable=False),
               ref("proposal_id", "proposals"), FK(["workspace_id"], ["workspaces.id"]))
intents = owned("execution_intents", C("proposal_id", S(120), nullable=False), C("provider_key", S(120), nullable=False),
                C("state", S(30), nullable=False), C("operation_id", S(200)), C("dispatched_at", Float),
                C("last_error", S(100)), ref("proposal_id", "proposals"), UQ("workspace_id", "proposal_id"), UQ("provider_key"))
attempts = owned("execution_attempts", C("intent_id", S(120), nullable=False), C("created_at", Float, nullable=False),
                 C("outcome", S(30), nullable=False), C("payload", JSON, nullable=False), ref("intent_id", "execution_intents"))
receipts = owned("receipts", C("proposal_id", S(120), nullable=False), C("intent_id", S(120), nullable=False),
                 C("created_at", Float, nullable=False), C("payload", JSON, nullable=False),
                 ref("proposal_id", "proposals"), ref("intent_id", "execution_intents"), UQ("workspace_id", "proposal_id"))
reviews = owned("reviews", C("proposal_id", S(120), nullable=False), C("state", S(30), nullable=False), C("revision", I, nullable=False),
                C("assigned_to", S(200), nullable=False), C("required_approvals", I, nullable=False), C("due_at", Float, nullable=False),
                C("binding_hash", S(64), nullable=False), C("input_id", S(120), nullable=False), C("rule", JSON, nullable=False),
                ref("proposal_id", "proposals"), ref("input_id", "decision_inputs"), UQ("workspace_id", "proposal_id"))
resolutions = owned("review_resolutions", C("review_id", S(120), nullable=False), C("actor_id", S(200), nullable=False),
                    C("resolution", S(20), nullable=False), C("comment", Text, nullable=False), C("binding_hash", S(64), nullable=False),
                    C("created_at", Float, nullable=False), ref("review_id", "reviews"), UQ("workspace_id", "review_id", "actor_id"))
jobs = owned("jobs", C("kind", S(30), nullable=False), C("logical_key", S(240), nullable=False), C("object_id", S(120), nullable=False),
             C("state", S(30), nullable=False), C("attempts", I, nullable=False), C("lease_owner", S(120)),
             C("lease_until", Float), C("available_at", Float, nullable=False), C("cancel_requested", Boolean, nullable=False),
             C("last_error", S(100)), C("created_at", Float, nullable=False), UQ("workspace_id", "logical_key"))
workers = Table("workers", metadata, C("id", S(120), primary_key=True), C("heartbeat_at", Float, nullable=False))
usage = Table("usage_counters", metadata, C("scope", S(120), primary_key=True), C("calls", B, nullable=False),
              C("tokens", B, nullable=False),
              C("reserved_calls", B, nullable=False, server_default="0"), C("reserved_tokens", B, nullable=False, server_default="0"),
              CK("calls >= 0 AND tokens >= 0 AND reserved_calls >= 0 AND reserved_tokens >= 0", name="nonnegative_usage"))
audit = owned("audit_events", C("actor_id", S(200), nullable=False), C("kind", S(100), nullable=False),
              C("resource_id", S(120), nullable=False), C("correlation_id", S(120), nullable=False), C("created_at", Float, nullable=False),
              C("payload_hash", S(64), nullable=False), C("payload", JSON, nullable=False))
experiments = owned("experiments", C("created_by", S(200), nullable=False), C("state", S(30), nullable=False),
                    C("manifest_hash", S(64), nullable=False), C("manifest", JSON, nullable=False), C("created_at", Float, nullable=False))
experiment_rows = owned("experiment_rows", C("experiment_id", S(120), nullable=False), C("case_id", S(120), nullable=False),
                        C("arm", S(120), nullable=False), C("payload", JSON, nullable=False), ref("experiment_id", "experiments"),
                        UQ("workspace_id", "experiment_id", "case_id", "arm"))
shadows = owned("shadow_subscriptions", C("policy_id", S(120), nullable=False), C("enabled", Boolean, nullable=False),
               C("created_at", Float, nullable=False), ref("policy_id", "policy_versions"))
shadow_rows = owned("shadow_results", C("subscription_id", S(120), nullable=False), C("input_id", S(120), nullable=False),
                   C("payload", JSON, nullable=False), ref("subscription_id", "shadow_subscriptions"), ref("input_id", "decision_inputs"),
                   UQ("workspace_id", "subscription_id", "input_id"))
legacy = owned("legacy_archives", C("source_id", S(120), nullable=False), C("source_hash", S(64), nullable=False),
               C("payload", JSON, nullable=False), C("receipt_count", I, nullable=False), C("spent_minor", B, nullable=False),
               UQ("workspace_id", "source_id", "source_hash"))
runs = owned("agent_runs", C("mandate_id", S(120), nullable=False), C("principal_id", S(200), nullable=False),
             C("request_id", S(120), nullable=False), C("state", S(30), nullable=False), C("revision", I, nullable=False),
             C("step", I, nullable=False), C("created_at", Float, nullable=False), C("payload", JSON, nullable=False),
             ref("mandate_id", "mandates"), UQ("workspace_id", "mandate_id", "request_id"))

model_requests = owned("model_requests", C("logical_key", S(240), nullable=False), C("attempt", I, nullable=False),
    C("resource_kind", S(30), nullable=False), C("resource_id", S(120), nullable=False), C("pool", S(20), nullable=False),
    C("backend", S(80), nullable=False), C("requested_model", S(120), nullable=False), C("resolved_model", S(120)),
    C("request_hash", S(64), nullable=False), C("request_payload", JSON, nullable=False), C("allowance_tokens", B, nullable=False),
    C("state", S(30), nullable=False), C("created_at", Float, nullable=False), C("deadline", Float, nullable=False), C("timeout_s", Float, nullable=False),
    C("dispatched_at", Float), C("charged_tokens", B, nullable=False), C("usage", JSON),
    C("response_hash", S(64)), C("response", JSON), C("error_code", S(80)),
    UQ("workspace_id", "logical_key", "attempt"), CK("attempt > 0 AND allowance_tokens > 0 AND charged_tokens >= 0", name="valid_usage"))
model_usage_events = owned("model_usage_events", C("request_id", S(120), nullable=False), C("kind", S(30), nullable=False),
    C("created_at", Float, nullable=False), C("payload", JSON, nullable=False), ref("request_id", "model_requests"))
