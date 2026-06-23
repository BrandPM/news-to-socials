"""``/api/v1/notifications`` route group — compute-on-the-fly (S5 Step 10).

The computation itself lives in :mod:`pipeline.admin.notifications_core` so
the Telegram push-alerter (:mod:`pipeline.monitoring.alerts`) shares the exact
same logic (NTS_073). This route is a thin brand-check + HTTP wrapper.

No persistence — the notification list is derived from existing tables on
each request. This keeps the data model unchanged and means deleting the
underlying row makes the notification disappear, which is the right
behavior for "action items" rather than "alerts."
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from pipeline.admin.db import session_scope
from pipeline.admin.models import Brand
from pipeline.admin.notifications_core import compute_notifications
from pipeline.admin.schemas import NotificationsListOut

router = APIRouter()


@router.get("", response_model=NotificationsListOut)
def list_notifications(
    brand_id: int = Query(..., description="Active brand id"),
) -> NotificationsListOut:
    with session_scope() as session:
        brand = session.get(Brand, brand_id)
        if brand is None:
            raise HTTPException(status_code=404, detail="brand not found")

        items = compute_notifications(session, brand_id)

    return NotificationsListOut(items=items, count=len(items))
