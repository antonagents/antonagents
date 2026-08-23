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

    run_now: bool = False   # optionally fire one run immediately on create


class AgentScheduleIn(BaseModel):
    schedule_kind: ScheduleKind = "once"
    # cron: a 5-field crontab expr ("0 9 * * *"); interval: seconds as string ("3600")
    schedule_expr: Optional[str] = None


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
