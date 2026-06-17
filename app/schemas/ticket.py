from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TicketType(str, Enum):
    EPIC = "Epic"
    STORY = "Story"
    TASK = "Task"


class RequirementType(str, Enum):
    PRODUCT = "Product"
    TECHNICAL = "Technical"
    CONFIGURATION = "Configuration"


class Priority(str, Enum):
    P1 = "P1 (Critical)"
    P2 = "P2 (High)"
    P3 = "P3 (Medium)"
    P4 = "P4 (Low)"


class TicketDescription(BaseModel):
    context: str = Field(..., description="Background and technical ecosystem")
    requirement_type: RequirementType
    requirement_content: str = Field(..., description="Detailed requirement per type")
    acceptance_criteria: str = Field(..., description="Gherkin-format acceptance criteria")


class JiraTicket(BaseModel):
    project_key: str
    issue_type: TicketType
    summary: str = Field(..., description="[System/Service Name] <Action or Capability>")
    description: TicketDescription
    priority: Priority = Priority.P3
    assignee: Optional[str] = None
    sprint: str = "Next Sprint"
    story_points: Optional[int] = None   # Story / Task only
    epic_link: Optional[str] = None      # Story / Task only
