"""Credential identities are derived server-side, never from action arguments."""
from dataclasses import dataclass
import hashlib
import hmac
import time
from sqlalchemy import select
from tollgate.storage import schema as s


@dataclass(frozen=True)
class Principal:
    actor_id: str
    kind: str = "human"
    credential_id: str | None = None
    workspace_id: str | None = None
    mandate_id: str | None = None


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: str
    principal: Principal
    role: str


PERMISSIONS = {
    "owner": {"read", "operate", "review", "admin", "policy", "lab"},
    "admin": {"read", "operate", "review", "admin", "policy", "lab"},
    "operator": {"read", "operate", "lab"},
    "reviewer": {"read", "review"},
    "auditor": {"read"},
    "agent": {"agent"},
}


def secret_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Identity:
    def __init__(self, database, settings, local_token=None):
        self.database, self.settings, self.local_token = database, settings, local_token
        self.jwks = None
        if settings.mode == "hosted":
            import jwt
            self.jwks = jwt.PyJWKClient(settings.identity_jwks_url, lifespan=60, timeout=5)

    def authenticate(self, token):
        if not token:
            raise PermissionError("Authentication required")
        if self.settings.mode == "local" and self.local_token and hmac.compare_digest(token, self.local_token):
            return Principal("local-owner")
        with self.database.transaction() as c:
            record = c.execute(select(s.credentials).where(s.credentials.c.secret_hash == secret_hash(token))).mappings().first()
        if record:
            if record["revoked"] or record["expires_at"] <= time.time():
                raise PermissionError("Credential unavailable")
            return Principal(record["principal_id"], "agent", record["id"], record["workspace_id"], record["mandate_id"])
        if self.jwks:
            import jwt
            try:
                key = self.jwks.get_signing_key_from_jwt(token)
                data = jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=self.settings.identity_audience,
                                  issuer=self.settings.identity_issuer, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
                if not isinstance(data["sub"], str) or not 1 <= len(data["sub"]) <= 180:
                    raise ValueError("Invalid subject")
                return Principal("jwt:" + data["sub"])
            except Exception:
                raise PermissionError("Credential unavailable") from None
        raise PermissionError("Credential unavailable")

    def context(self, c, principal, wid, permission, mandate_id=None):
        workspace = c.execute(select(s.workspaces).where(s.workspaces.c.id == wid)).mappings().first()
        if not workspace or workspace["expires_at"] <= time.time():
            raise LookupError("Resource unavailable")
        if principal.kind == "service":
            run = c.execute(select(s.runs).where(s.runs.c.workspace_id == wid,
                s.runs.c.principal_id == principal.actor_id, s.runs.c.mandate_id == mandate_id,
                s.runs.c.state.in_(["running", "waiting", "waiting_for_review"]))).first()
            if not run or permission != "agent" or principal.workspace_id != wid or principal.mandate_id != mandate_id:
                raise LookupError("Resource unavailable")
            return WorkspaceContext(wid, principal, "agent")
        if principal.kind == "agent":
            credential = c.execute(select(s.credentials).where(s.credentials.c.workspace_id == wid,
                                                              s.credentials.c.id == principal.credential_id)).mappings().first()
            if (not credential or credential["revoked"] or credential["expires_at"] <= time.time()
                    or principal.workspace_id != wid or permission != "agent"
                    or (mandate_id and principal.mandate_id != mandate_id)):
                raise LookupError("Resource unavailable")
            return WorkspaceContext(wid, principal, "agent")
        member = c.execute(select(s.memberships).where(s.memberships.c.workspace_id == wid,
                   s.memberships.c.actor_id == principal.actor_id, s.memberships.c.active.is_(True))).mappings().first()
        if not member or permission not in PERMISSIONS.get(member["role"], set()):
            raise LookupError("Resource unavailable")
        return WorkspaceContext(wid, principal, member["role"])
