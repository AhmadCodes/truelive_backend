"""
Responses of the deploy routes (plan 06 D14; NEW-45, NEW-46).

They carry outcomes, never the generated layout: the layout holds camera URLs
with credentials. ``response_model`` drops any other key a caller might add.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class PCDeployResult(BaseModel):
    """What one deploy did for one PC."""

    pc_id: str
    status: Literal["deployed", "skipped", "failed"] = Field(
        ..., description="deployed: handed to the PC's relay session; failed: not delivered; skipped: not sent"
    )
    delivery: Optional[Literal["delivered", "offline", "error"]] = Field(
        None, description="The relay's answer (None when nothing was sent)"
    )
    config_id: Optional[str] = Field(None, description="The deploy's id (pc_deployments.id); None when no row")
    confirmable: bool = Field(
        False, description="The station can confirm applied (Client >= 1.4.0); 1.1.x rows end at received"
    )
    message: str
    screens: int = 0
    ack_timeout_sec: Optional[int] = Field(
        None,
        description=(
            "DEPLOY_ACK_TIMEOUT_SEC of this backend: how long the relay waits for the station before the row "
            "becomes unconfirmed (the portal polls the row until this plus 60 s, M2 review)"
        ),
    )


class DeploySummary(BaseModel):
    total: int
    deployed: int
    skipped: int
    failed: int
    results: List[PCDeployResult]


class LayoutDeployResponse(DeploySummary):
    screen_layout_id: str
    message: Optional[str] = None
