"""Pydantic request/response schemas for the API."""
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field

ScheduleKind = Literal["once", "cron", "interval"]


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=200)
    name: Optional[str] = Field(None, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=200)


class DbConnectionTestIn(BaseModel):
    engine: Literal["postgres", "mysql", "mongo"]
    dsn: str = Field(..., min_length=1, max_length=2000)
    mode: Literal["read_only", "read_write"] = "read_only"


class DbConnectionUpdateIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    dsn: Optional[str] = Field(None, min_length=1, max_length=2000)
    mode: Optional[Literal["read_only", "read_write"]] = None


class OrgSwitchIn(BaseModel):
    org_id: int


class InvitationIn(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"] = "member"


class MemberRoleIn(BaseModel):
    role: Literal["admin", "member"]


class SignupInviteIn(BaseModel):
    email: EmailStr


class ReplyIn(BaseModel):
    prompt: str = Field(..., min_length=1)


class ApiKeyIn(BaseModel):
    name: Optional[str] = Field(None, max_length=120)


class V1ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=100000)


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    # default instruction the agent runs on a manual trigger or scheduled fire;
    # a chat turn overrides it with the user's message.
    prompt: str = Field("", max_length=20000)

    # scheduling
    schedule_kind: ScheduleKind = "once"
    # cron: a 5-field crontab expr ("0 9 * * *"); interval: seconds as string ("3600")
    schedule_expr: Optional[str] = None

    # per-agent access + model options
    env: dict[str, str] = Field(default_factory=dict)
    provider: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    max_turns: Optional[int] = None
    # manual minutes this agent's run replaces — powers the impact view
    baseline_minutes: Optional[int] = Field(None, ge=0, le=100000)
    # per-run wall-clock override in seconds; clamped server-side, NULL = global default
    timeout_secs: Optional[int] = Field(None, ge=0, le=100000)

    run_now: bool = False   # optionally fire one run immediately on create


class AgentScheduleIn(BaseModel):
    schedule_kind: ScheduleKind = "once"
    # cron: a 5-field crontab expr ("0 9 * * *"); interval: seconds as string ("3600")
    schedule_expr: Optional[str] = None
    # optional: also update the routine's max runtime from the same modal
    timeout_secs: Optional[int] = Field(None, ge=0, le=100000)


class AgentNameIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class AgentModelIn(BaseModel):
    provider: Optional[str] = Field(None, max_length=60)
    model: Optional[str] = Field(None, max_length=120)


class AgentConnectorsIn(BaseModel):
    providers: list[str] = Field(default_factory=list)


class AgentSkillsIn(BaseModel):
    skill_ids: list[int] = Field(default_factory=list)


class SkillIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    instructions: str = Field("", max_length=20000)


AlertKind = Literal["on_failure", "on_success", "result_contains", "schedule_miss"]


class AlertRuleIn(BaseModel):
    kind: AlertKind
    # substring to match against the run's result (used only by result_contains)
    condition: Optional[str] = Field(None, max_length=500)
    enabled: bool = True


class AgentAlertsIn(BaseModel):
    rules: list[AlertRuleIn] = Field(default_factory=list)


class NotificationsReadIn(BaseModel):
    # specific notification ids to mark read; omit/null marks all of the org's read
    ids: Optional[list[int]] = None


class DbConnectionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    engine: Literal["postgres", "mysql", "mongo"]
    dsn: str = Field(..., min_length=1, max_length=2000)
    mode: Literal["read_only", "read_write"] = "read_only"


class AgentDbConnectionsIn(BaseModel):
    db_connection_ids: list[int] = Field(default_factory=list)


# ---------------- outcome jobs (docs/features/outcome-jobs.md) ----------------

class JobCheckIn(BaseModel):
    kind: Literal["sql", "artifact", "judge", "human"]
    name: str = Field(..., min_length=1, max_length=200)
    config: dict = Field(default_factory=dict)
    required: bool = True


class JobSpecIn(BaseModel):
    objective: str = Field(..., min_length=1, max_length=2000)
    checks: list[JobCheckIn] = Field(..., min_length=1, max_length=20)
    deliver_to: Optional[list] = None
    constraints: Optional[list] = None
    budget: Optional[dict] = None
    escalate_to: Optional[list] = None      # None = default (owner email); [] = in-app only
    publish: bool = False


class SpecDraftIn(BaseModel):
    description: str = Field(..., min_length=5, max_length=4000)


class TestChecksIn(BaseModel):
    checks: list[JobCheckIn] = Field(..., min_length=1, max_length=20)


class ResolveIn(BaseModel):
    action: Literal["retry", "answer", "dismiss"]
    message: Optional[str] = Field(None, max_length=2000)


class FlagIn(BaseModel):
    """'This outcome was wrong' — feeds the false-delivered rate."""
    note: str = Field("", max_length=1000)


class BaselineIn(BaseModel):
    minutes: Optional[int] = Field(None, ge=0, le=100000)


class ReviewIn(BaseModel):
    """A person's verdict on an outcome awaiting approval or picked for a spot-check."""
    decision: Literal["approve", "changes", "reject"]
    note: str = Field("", max_length=2000)


class JobPlanIn(BaseModel):
    """Delegate a job: plain-language description, or a chat that already did it."""
    description: str = Field("", max_length=4000)
    from_chat_id: Optional[int] = None


class JobCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=20000)
    schedule_kind: Literal["once", "cron", "interval"] = "once"
    schedule_expr: Optional[str] = None
    connectors: list[str] = Field(default_factory=list)
    db_connection_ids: list[int] = Field(default_factory=list)
    spec: JobSpecIn
    mode: Literal["live", "dry_run", "draft"] = "dry_run"
    from_chat_id: Optional[int] = None     # copy that chat's workspace files
    baseline_minutes: Optional[int] = Field(None, ge=0, le=100000)


class GuardrailsIn(BaseModel):
    """A routine's overrides: {category: allow|approve|block} and per-tool rules."""
    categories: dict = Field(default_factory=dict)
    tools: dict = Field(default_factory=dict)


class ActionDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = Field("", max_length=1000)
