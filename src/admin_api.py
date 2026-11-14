"""
Admin API endpoints for the management console.
Provides JSON data for Dashboard, Customers, Conversations, Follow-ups, and Bridge Status.
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
import csv
import io

import openpyxl

logger = logging.getLogger(__name__)
from fastapi import APIRouter, File, Query, UploadFile

from config import get_config, get_data_dir
from database import (
    get_connection, update_customer, get_daily_quota_usage,
    create_order_record, update_order_record, delete_order_record,
)
from qr_state import get_pending_qr, clear_pending_qr
from bot_state import (
    is_paused, toggle_paused, set_paused,
    is_proactive_followups_enabled, is_proactive_followups_configured,
    set_proactive_followups_enabled,
)
from send_service import check_proactive_eligibility, send_followup_now, send_manual_message

_ocr_instance = None

def _get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        from rapidocr import RapidOCR
        _ocr_instance = RapidOCR()
    return _ocr_instance

router = APIRouter(prefix="/admin/api")


# ── Setup ──────────────────────────────────────────────────────────────────────

@router.get("/setup-status")
async def setup_status():
    """Check if the app is configured (API key set in .env file)."""
    data_dir = get_data_dir()
    env_path = data_dir / ".env"
    has_key = False
    if env_path.exists():
        for line in env_path.read_text().split("\n"):
            line = line.strip()
            if line.startswith("LLM_API_KEY="):
                val = line.partition("=")[2].strip().strip('"').strip("'")
                if val:
                    has_key = True
                break
    return {"configured": has_key}


@router.post("/setup")
async def save_setup(data: dict):
    """Save initial configuration (API key)."""
    api_key = data.get("api_key", "").strip()
    if not api_key:
        return {"error": "api_key required"}

    data_dir = get_data_dir()
    env_path = data_dir / ".env"

    # Read existing .env or create new one
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().split("\n"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip().strip('"').strip("'")

    existing["LLM_API_KEY"] = api_key

    # Write back
    lines = [f"{k}={v}" for k, v in existing.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Refresh tenant env cache; only touch process-wide os.environ in single-tenant mode.
    import tenancy
    tenancy.invalidate_env(data_dir)
    if tenancy.current() is None:
        os.environ["LLM_API_KEY"] = api_key

    return {"ok": True}


# ── Dashboard ──────────────────────────────────────────────────────────────────

def _dashboard_sync() -> dict:
    from workspace_service import workspace_overview
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat()

    total = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM customers WHERE status='active'").fetchone()[0]

    # Today's stats. Only confirmed sends count as replies; pending/unknown rows
    # must not inflate the dashboard.
    today_ai_replies = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE ai_generated=1 AND direction='outbound' "
        "AND status='sent' AND date(sent_at)=?",
        (today,)
    ).fetchone()[0]
    today_inbound = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE direction='inbound' AND date(sent_at)=?",
        (today,)
    ).fetchone()[0]

    # Follow-up stats
    due_today = conn.execute(
        "SELECT COUNT(*) FROM follow_up_schedule fs JOIN customers c ON fs.customer_id=c.id "
        "WHERE fs.active=1 AND c.status='active' AND date(fs.next_followup_at)=?", (today,)
    ).fetchone()[0]
    due_overdue = conn.execute(
        "SELECT COUNT(*) FROM follow_up_schedule fs JOIN customers c ON fs.customer_id=c.id "
        "WHERE fs.active=1 AND c.status='active' AND fs.next_followup_at <= ?", (now_iso,)
    ).fetchone()[0]
    sent_fu_total = conn.execute("SELECT COUNT(*) FROM sent_followups WHERE status='sent'").fetchone()[0]
    sent_fu_today = conn.execute(
        "SELECT COUNT(*) FROM sent_followups WHERE status='sent' AND date(sent_at)=?", (today,)
    ).fetchone()[0]
    failed_fu_today = conn.execute(
        "SELECT COUNT(*) FROM sent_followups WHERE status='failed' AND date(sent_at)=?", (today,)
    ).fetchone()[0]
    unknown_fu_today = conn.execute(
        "SELECT COUNT(*) FROM sent_followups WHERE status='unknown' AND date(sent_at)=?", (today,)
    ).fetchone()[0]

    total_schedules = conn.execute("SELECT COUNT(*) FROM follow_up_schedule WHERE active=1").fetchone()[0]
    total_conv = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

    # Recently failed sends
    failed_recent = conn.execute(
        "SELECT c.id AS customer_id, c.name, c.phone, sf.error_message, sf.sent_at, sf.status FROM sent_followups sf "
        "JOIN customers c ON sf.customer_id=c.id "
        "WHERE sf.status IN ('failed','unknown') ORDER BY sf.sent_at DESC LIMIT 5"
    ).fetchall()

    # Recently active customers
    recent_active = conn.execute(
        "SELECT c.id, c.name, c.phone, c.company, c.status, "
        "MAX(conv.sent_at) as last_contact FROM customers c "
        "JOIN conversations conv ON c.id=conv.customer_id "
        "GROUP BY c.id ORDER BY last_contact DESC LIMIT 10"
    ).fetchall()

    # Upcoming schedule
    upcoming = conn.execute(
        "SELECT date(fs.next_followup_at) as dt, COUNT(*) as cnt FROM follow_up_schedule fs "
        "JOIN customers c ON fs.customer_id=c.id "
        "WHERE fs.active=1 AND c.status='active' AND date(fs.next_followup_at) >= ? "
        "GROUP BY dt ORDER BY dt LIMIT 14", (today,)
    ).fetchall()

    return {
        "today": today,
        "counts": {
            "total_customers": total, "active_customers": active,
            "today_ai_replies": today_ai_replies, "today_inbound": today_inbound,
            "due_today": due_today, "due_overdue": due_overdue,
            "sent_fu_total": sent_fu_total, "sent_fu_today": sent_fu_today,
            "failed_fu_today": failed_fu_today, "unknown_fu_today": unknown_fu_today,
            "total_schedules": total_schedules, "total_conversations": total_conv,
        },
        "failed_recent": [dict(r) for r in failed_recent],
        "recent_active": [
            {"id": r["id"], "name": r["name"], "phone": r["phone"],
             "company": r["company"], "status": r["status"], "last_contact": r["last_contact"]}
            for r in recent_active
        ],
        "upcoming": [{"date": r["dt"], "count": r["cnt"]} for r in upcoming],
        "workspace": workspace_overview(),
    }


@router.get("/dashboard")
async def dashboard():
    data, bridge = await asyncio.gather(
        asyncio.to_thread(_dashboard_sync),
        _get_bridge_status(),
    )
    data["bridge"] = bridge
    return data


# ── Customers ──────────────────────────────────────────────────────────────────

def _customers_sync(page: int, per_page: int, search: str, status: str, sort: str, order: str) -> dict:
    conn = get_connection()
    allowed_sorts = {"name", "company", "status", "created_at", "updated_at"}
    if sort not in allowed_sorts:
        sort = "updated_at"
    if order not in ("asc", "desc"):
        order = "desc"

    where = ["1=1"]
    params = []
    if search:
        where.append("(c.name LIKE ? OR c.phone LIKE ? OR c.company LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s])
    if status:
        where.append("c.status = ?")
        params.append(status)

    where_clause = " AND ".join(where)

    total = conn.execute(f"SELECT COUNT(*) FROM customers c WHERE {where_clause}", params).fetchone()[0]

    params_with_limit = params + [per_page, (page - 1) * per_page]
    rows = conn.execute(
        f"""SELECT c.*,
                   (SELECT COUNT(*) FROM conversations WHERE customer_id=c.id) as conv_count,
                   (SELECT MAX(sent_at) FROM conversations WHERE customer_id=c.id) as last_msg,
                   (SELECT next_followup_at FROM follow_up_schedule WHERE customer_id=c.id AND active=1 LIMIT 1) as next_fu
            FROM customers c WHERE {where_clause}
            ORDER BY c.{sort} {order} LIMIT ? OFFSET ?""",
        params_with_limit
    ).fetchall()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [dict(r) for r in rows],
    }


@router.get("/customers")
async def customers(
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=5, le=200),
    search: str = Query(""),
    status: str = Query(""),
    sort: str = Query("updated_at"),
    order: str = Query("desc"),
):
    return await asyncio.to_thread(_customers_sync, page, per_page, search, status, sort, order)


# ── Invalid Phone Detection & Cleanup (must be BEFORE {customer_id} route) ──

@router.get("/customers/invalid-phones")
async def list_invalid_phones():
    """Detect customers whose phone field contains names instead of phone numbers."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT c.id, c.phone, c.name, c.status, c.company, c.notes,
                  (SELECT COUNT(*) FROM conversations WHERE customer_id=c.id) as conv_count,
                  (SELECT COUNT(*) FROM follow_up_schedule WHERE customer_id=c.id AND active=1) as active_fu_count
           FROM customers c
           WHERE c.phone GLOB '*[A-Za-z]*' AND c.phone NOT GLOB '+*'
           ORDER BY c.status, c.id"""
    ).fetchall()

    items = [dict(r) for r in rows]
    total = len(items)
    with_conv = sum(1 for r in items if r["conv_count"] > 0)
    with_fu = sum(1 for r in items if r["active_fu_count"] > 0)
    safe_to_delete = sum(1 for r in items if r["conv_count"] == 0)

    return {
        "total_invalid": total,
        "with_conversations": with_conv,
        "with_active_followup": with_fu,
        "safe_to_delete": safe_to_delete,
        "items": items,
    }


@router.post("/customers/cleanup-invalid-phones")
async def cleanup_invalid_phones(data: dict):
    """Batch cleanup invalid phone numbers.

    Actions:
      - "detect_only": return list without making changes
      - "delete_safe": delete customers with invalid phones AND no conversations/orders
      - "mark_inactive": mark customers with invalid phones as inactive (if they have conversations)
    """
    from database import cleanup_invalid_phone_customers
    action = data.get("action", "detect_only")
    return await asyncio.to_thread(cleanup_invalid_phone_customers, action)


@router.get("/customers/{customer_id}")
async def customer_detail(customer_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not row:
        return {"error": "not found"}

    convs = conn.execute(
        "SELECT * FROM conversations WHERE customer_id=? ORDER BY sent_at DESC LIMIT 50",
        (customer_id,)
    ).fetchall()

    fu = conn.execute(
        "SELECT * FROM follow_up_schedule WHERE customer_id=? AND active=1 LIMIT 1",
        (customer_id,)
    ).fetchone()

    sent_fus = conn.execute(
        "SELECT * FROM sent_followups WHERE customer_id=? ORDER BY sent_at DESC LIMIT 10",
        (customer_id,)
    ).fetchall()

    return {
        "customer": dict(row),
        "conversations": [dict(c) for c in reversed(convs)],
        "followup_schedule": dict(fu) if fu else None,
        "sent_followups": [dict(s) for s in sent_fus],
    }


@router.post("/customers/{customer_id}/status")
async def update_customer_status(customer_id: int, data: dict):
    allowed = {"status", "notes", "tags", "name", "company", "phone", "email"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return {"error": "no valid fields"}
    row = get_connection().execute("SELECT phone FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not row:
        return {"error": "not found"}
    new_phone = updates.pop("phone", None)
    try:
        result = update_customer(row["phone"], new_phone=new_phone, **updates)
    except Exception as e:
        err_msg = str(e)
        if "UNIQUE constraint" in err_msg and "customers.phone" in err_msg and new_phone:
            # ── Smart merge: reassign data from the phone owner to current customer ──
            existing = get_connection().execute(
                "SELECT * FROM customers WHERE phone=?", (new_phone,)
            ).fetchone()
            if existing and existing["id"] != customer_id:
                from database import merge_customers
                merged = merge_customers(customer_id, existing["id"])
                if merged:
                    logger.info(
                        f"Merged customer #{existing['id']} ({existing['name']}, {existing['phone']}) "
                        f"into #{customer_id} — all related records reassigned, old record deleted"
                    )
                # Now the phone is free — retry the update
                try:
                    result = update_customer(row["phone"], new_phone=new_phone, **updates)
                except Exception as e2:
                    return {"ok": False, "error": f"合并后更新失败：{e2}"}
                return {
                    "ok": True,
                    "customer": result,
                    "merged_from": merged or {
                        "id": existing["id"],
                        "name": existing["name"],
                        "phone": existing["phone"],
                    },
                }
            return {"ok": False, "error": f"电话号码 {new_phone} 已存在，无法保存。请检查是否与其他客户重复。"}
        return {"ok": False, "error": f"保存失败：{err_msg}"}
    if result is None and new_phone:
        return {"ok": False, "error": f"更新失败，电话号码可能已被占用或客户不存在。"}
    return {"ok": True, "customer": result}


NAME_KEYS = ["姓名", "name", "名称"]
PHONE_KEYS = ["电话", "phone", "手机", "手机号", "电话号码", "tel", "mobile"]
EMAIL_KEYS = ["邮箱", "email", "e-mail", "邮件"]
NOTES_KEYS = ["备注", "notes", "note", "说明", "remark"]
INFO_KEYS = ["客户具体信息", "联系人信息", "contact info", "contact"]
COMPANY_KEYS = ["公司名", "公司名、网址", "公司", "company", "公司名称"]


def _parse_info_field(raw: str) -> dict:
    """Parse N:/E:/M: fields from a combined info column like '客户具体信息'."""
    result = {"name": "", "email": "", "phone": ""}
    if not raw:
        return result
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            prefix, _, value = line.partition(":")
            prefix = prefix.strip().upper()
            value = value.strip()
            if prefix == "N" and not result["name"]:
                result["name"] = value
            elif prefix == "E" and not result["email"]:
                result["email"] = value
            elif prefix == "M" and not result["phone"]:
                result["phone"] = value
    return result


def _find_column(headers: list, keys: list) -> str | None:
    """Find the first header that matches one of the given keys (case-insensitive)."""
    key_set = {k.lower() for k in keys}
    for h in headers:
        hl = h.strip().lower()
        if hl in key_set:
            return h
    return None


def _build_col_map(headers: list):
    """Build column mapping from headers. Returns (col_map, has_info_col)."""
    col_map = {}
    has_info_col = False
    for h in headers:
        hl = h.strip().lower()
        if _find_column([h], NAME_KEYS):
            col_map["name"] = h
        elif _find_column([h], PHONE_KEYS):
            col_map["phone"] = h
        elif _find_column([h], EMAIL_KEYS):
            col_map["email"] = h
        elif _find_column([h], NOTES_KEYS):
            col_map["notes"] = h
        elif _find_column([h], COMPANY_KEYS):
            col_map["company"] = h
        elif _find_column([h], INFO_KEYS):
            col_map["info"] = h
            has_info_col = True
    return col_map, has_info_col


def _import_customers_sync(filename: str, raw_bytes: bytes) -> dict:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    row_offset = 2  # default for CSV (header row 1, data from row 2)

    if ext == "csv":
        content = raw_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            return {"error": "文件为空或无法解析"}
        headers = list(rows[0].keys())
    elif ext in ("xlsx", "xls"):
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True)
        ws = wb.active
        max_scan = min(5, ws.max_row or 5)
        preview_rows = []
        for row in ws.iter_rows(min_row=1, max_row=max_scan, values_only=True):
            preview_rows.append([str(c or "").strip() for c in row])

        best_row_idx = 0
        best_score = 0
        all_known = NAME_KEYS + PHONE_KEYS + EMAIL_KEYS + NOTES_KEYS + COMPANY_KEYS + INFO_KEYS
        all_lower = {k.lower() for k in all_known}
        for idx, row_data in enumerate(preview_rows):
            score = sum(1 for c in row_data if c.lower() in all_lower)
            if score > best_score:
                best_score = score
                best_row_idx = idx

        if best_score == 0:
            wb.close()
            return {"error": "未找到可识别的表头列，请确保表格包含姓名/电话/客户具体信息等列"}

        headers = preview_rows[best_row_idx]
        row_offset = best_row_idx + 2
        rows = [dict(zip(headers, [str(c or "") if c is not None else "" for c in row]))
                for row in ws.iter_rows(min_row=row_offset, values_only=True)]
        wb.close()
    else:
        return {"error": "仅支持 .xlsx 或 .csv 文件"}

    if not rows:
        return {"error": "文件为空或无法解析"}

    col_map, has_info_col = _build_col_map(headers)

    if "phone" not in col_map and not has_info_col:
        return {"error": "未找到电话/手机列，请确保表头包含'电话'或'phone'列，或包含'客户具体信息'列"}

    from database import db_write
    imported = 0
    skipped = []

    with db_write() as conn:
        for i, row in enumerate(rows):
            row_num = i + row_offset
            if has_info_col:
                info_raw = (row.get(col_map.get("info", ""), "") or "").strip()
                parsed = _parse_info_field(info_raw)
                phone_raw = parsed["phone"]
                name = parsed["name"]
                email = parsed["email"]
            else:
                phone_raw = (row.get(col_map.get("phone", ""), "") or "").strip()
                name = (row.get(col_map.get("name", ""), "") or "").strip()
                email = (row.get(col_map.get("email", ""), "") or "").strip()

            if "name" in col_map:
                explicit_name = (row.get(col_map["name"], "") or "").strip()
                if explicit_name:
                    name = explicit_name
            if "email" in col_map:
                explicit_email = (row.get(col_map["email"], "") or "").strip()
                if explicit_email:
                    email = explicit_email

            phone = ''.join(c for c in phone_raw if c.isdigit() or c == '+')
            if not phone:
                skipped.append({"row": row_num, "reason": "电话为空"})
                continue

            if not name:
                skipped.append({"row": row_num, "reason": "姓名为空"})
                continue

            existing = conn.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
            if existing:
                skipped.append({"row": row_num, "reason": f"电话 {phone} 已存在"})
                continue

            notes = (row.get(col_map.get("notes", ""), "") or "").strip()
            company = (row.get(col_map.get("company", ""), "") or "").strip()

            try:
                conn.execute(
                    "INSERT INTO customers(phone, name, email, company, notes) VALUES(?,?,?,?,?)",
                    (phone, name, email, company, notes))
                imported += 1
            except Exception as e:
                skipped.append({"row": row_num, "reason": f"写入失败: {str(e)}"})

    return {"imported": imported, "skipped": skipped, "total": len(rows)}


@router.post("/customers/import")
async def import_customers(file: UploadFile = File(...)):
    filename = file.filename or ""
    raw_bytes = await file.read()
    if len(raw_bytes) > 20 * 1024 * 1024:
        return {"error": "导入文件过大（最大 20MB）"}
    return await asyncio.to_thread(_import_customers_sync, filename, raw_bytes)


# ── Conversations ──────────────────────────────────────────────────────────────

@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int):
    from database import db_write
    with db_write() as conn:
        row = conn.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            return {"error": "not found"}
        conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
    return {"ok": True}


@router.delete("/customers/{customer_id}")
async def delete_customer(customer_id: int):
    """Delete a customer and all related records (conversations, followup schedule, orders, scores, memory)."""
    from database import delete_customer_cascade
    deleted = delete_customer_cascade(customer_id)
    if not deleted:
        return {"ok": False, "error": "客户不存在"}
    logger.info(f"Deleted customer #{customer_id}: {deleted['name']} ({deleted['phone']}) — all related records removed")
    return {"ok": True, "deleted": deleted}


@router.delete("/customers/{customer_id}/conversations")
async def delete_customer_conversations(customer_id: int):
    from database import db_write
    with db_write() as conn:
        conn.execute("DELETE FROM conversations WHERE customer_id=?", (customer_id,))
    return {"ok": True}


@router.get("/conversations")
async def conversations_list(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=50),
    customer_id: int = Query(None),
):
    conn = get_connection()
    if customer_id:
        total = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE customer_id=?", (customer_id,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT conv.*, c.name, c.phone FROM conversations conv "
            "JOIN customers c ON conv.customer_id=c.id "
            "WHERE conv.customer_id=? ORDER BY conv.sent_at DESC LIMIT ? OFFSET ?",
            (customer_id, per_page, (page - 1) * per_page)
        ).fetchall()
    else:
        total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        rows = conn.execute(
            "SELECT conv.*, c.name, c.phone FROM conversations conv "
            "JOIN customers c ON conv.customer_id=c.id "
            "ORDER BY conv.sent_at DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page)
        ).fetchall()

    return {
        "total": total, "page": page, "per_page": per_page,
        "items": [dict(r) for r in rows],
    }


def _followups_sync(filter_type: str) -> dict:
    conn = get_connection()
    now_iso = datetime.now().isoformat()
    today = datetime.now().strftime("%Y-%m-%d")

    if filter_type == "due":
        rows = conn.execute(
            "SELECT fs.*, c.name, c.phone, c.company, c.notes FROM follow_up_schedule fs "
            "JOIN customers c ON fs.customer_id=c.id "
            "WHERE fs.active=1 AND c.status='active' AND fs.next_followup_at <= ? "
            "ORDER BY fs.next_followup_at LIMIT 100", (now_iso,)
        ).fetchall()
    elif filter_type == "today_sent":
        rows = conn.execute(
            "SELECT sf.*, c.name, c.phone FROM sent_followups sf "
            "JOIN customers c ON sf.customer_id=c.id "
            "WHERE sf.status='sent' AND date(sf.sent_at)=? ORDER BY sf.sent_at DESC LIMIT 50",
            (today,)
        ).fetchall()
    elif filter_type == "failed":
        rows = conn.execute(
            "SELECT sf.*, c.name, c.phone FROM sent_followups sf "
            "JOIN customers c ON sf.customer_id=c.id "
            "WHERE sf.status='failed' ORDER BY sf.sent_at DESC LIMIT 50"
        ).fetchall()
    elif filter_type == "unknown":
        rows = conn.execute(
            "SELECT sf.*, c.name, c.phone FROM sent_followups sf "
            "JOIN customers c ON sf.customer_id=c.id "
            "WHERE sf.status='unknown' ORDER BY sf.sent_at DESC LIMIT 50"
        ).fetchall()
    elif filter_type == "paused":
        rows = conn.execute(
            "SELECT fs.*, c.name, c.phone, c.company FROM follow_up_schedule fs "
            "JOIN customers c ON fs.customer_id=c.id "
            "WHERE fs.active=0 ORDER BY fs.id DESC LIMIT 50"
        ).fetchall()
    else:
        rows = []

    return {"filter": filter_type, "items": [dict(r) for r in rows]}


@router.get("/customers-with-conversations")
async def customers_with_conversations():
    from workspace_service import conversation_workspace
    return await asyncio.to_thread(conversation_workspace)


# ── Follow-ups ─────────────────────────────────────────────────────────────────

@router.get("/followups")
async def followups(
    filter_type: str = Query("due", description="due | today_sent | failed | unknown | paused"),
):
    return await asyncio.to_thread(_followups_sync, filter_type)


@router.get("/followups/by-date")
async def followups_by_date(date: str = Query(...)):
    conn = get_connection()
    rows = conn.execute(
        "SELECT fs.*, c.name, c.phone, c.company, c.notes FROM follow_up_schedule fs "
        "JOIN customers c ON fs.customer_id=c.id "
        "WHERE fs.active=1 AND c.status='active' AND date(fs.next_followup_at)=? "
        "ORDER BY fs.next_followup_at", (date,)
    ).fetchall()
    return {"date": date, "items": [dict(r) for r in rows]}


@router.post("/followups/{schedule_id}/toggle")
async def toggle_followup(schedule_id: int):
    from database import db_write
    with db_write() as conn:
        row = conn.execute("SELECT active FROM follow_up_schedule WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            return {"error": "not found"}
        new_active = 0 if row["active"] else 1
        conn.execute("UPDATE follow_up_schedule SET active=? WHERE id=?", (new_active, schedule_id))
    return {"ok": True, "active": new_active}


@router.post("/followups/{schedule_id}/send-now")
async def send_followup_now_endpoint(schedule_id: int, data: dict | None = None):
    """Manually trigger one follow-up through the unified send service.

    The same eligibility checks and atomic daily quota as the schedulers are
    used. When a check is overridable (for example the automatic switch is off),
    the response includes ``overridable_reasons``; the caller must retry with
    an explicit ``overrides`` list. Quota, pause, contact status and inactive
    schedules can never be overridden.
    """
    data = data or {}
    overrides = data.get("overrides") or []
    if not isinstance(overrides, list):
        return {"ok": False, "status": "invalid_request",
                "error": "overrides must be a list"}
    result = await send_followup_now(schedule_id, manual=True,
                                     overrides=[str(x) for x in overrides])
    result["overrides_requested"] = overrides
    return result


@router.post("/send-manual")
async def send_manual(data: dict):
    """Send a human-written chat message.

    This is a human conversation action, not an automated follow-up: it is
    allowed while the bot is paused and does not consume the proactive quota.
    It still records pending/sent/failed/unknown consistently.
    """
    phone = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()
    customer_id = data.get("customer_id")
    if not phone or not message:
        return {"ok": False, "status": "invalid_request",
                "error": "phone and message required"}

    try:
        customer_id = int(customer_id) if customer_id else None
    except (TypeError, ValueError):
        return {"ok": False, "status": "invalid_request", "error": "invalid customer_id"}

    conn = get_connection()
    if customer_id is not None:
        if not conn.execute("SELECT id FROM customers WHERE id=?", (customer_id,)).fetchone():
            return {"ok": False, "status": "invalid_request", "error": "customer not found"}
    else:
        row = conn.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
        if row:
            customer_id = row["id"]

    try:
        result = await send_manual_message(customer_id, phone, message)
    except Exception as e:
        logger.error("Manual send failed: %s", e, exc_info=True)
        return {"ok": False, "status": "failed", "error": str(e)}
    result["bridge_result"] = {
        "status": result.get("status"),
        "message_id": result.get("message_id"),
        "error": result.get("error"),
    }
    return result


# ── Batch Push helpers ────────────────────────────────────────────────────────

@router.post("/followups/add-to-batch")
async def add_customer_to_batch(data: dict):
    """Add a customer to followup schedule for batch push."""
    from database import ensure_active_batch_schedule
    customer_id = data.get("customer_id")
    if not customer_id:
        return {"error": "customer_id required"}
    return ensure_active_batch_schedule(int(customer_id))


@router.post("/followups/{schedule_id}/remove-from-batch")
async def remove_customer_from_batch(schedule_id: int):
    """Remove a customer from the batch by deactivating their followup schedule."""
    from database import db_write
    with db_write() as conn:
        row = conn.execute("SELECT id FROM follow_up_schedule WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            return {"error": "not found"}
        conn.execute("UPDATE follow_up_schedule SET active=0 WHERE id=?", (schedule_id,))
    return {"ok": True}


def _due_schedule_ids(schedule_ids: list | None = None) -> list[dict]:
    """Return active, due schedules (optionally restricted to selected ids)."""
    conn = get_connection()
    now_iso = datetime.now().isoformat()
    sql = (
        "SELECT fs.*, c.name, c.phone, c.company, c.status AS customer_status "
        "FROM follow_up_schedule fs JOIN customers c ON fs.customer_id=c.id "
        "WHERE fs.active=1 AND c.status='active' AND fs.next_followup_at<=?"
    )
    params: list = [now_iso]
    if schedule_ids is not None:
        cleaned = []
        for sid in schedule_ids:
            try:
                cleaned.append(int(sid))
            except (TypeError, ValueError):
                continue
        if not cleaned:
            return []
        sql += " AND fs.id IN (" + ",".join("?" for _ in cleaned) + ")"
        params.extend(cleaned)
    sql += " ORDER BY fs.next_followup_at"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


@router.post("/followups/batch/start")
async def start_batch_push(data: dict | None = None):
    """Confirm and start a manual batch.

    Only a confirmed, non-empty, eligible batch updates today's
    ``last_batch_push_date`` marker. Cancelling in the UI never calls this
    endpoint; an empty queue, a pause, an exhausted quota or a non-overridable
    blocker returns without marking the day.
    """
    data = data or {}
    if data.get("confirmed") is not True:
        return {"ok": False, "status": "not_confirmed", "marked": False,
                "error": "用户尚未确认开始批量推送"}
    overrides = data.get("overrides") or []
    if not isinstance(overrides, list):
        return {"ok": False, "status": "invalid_request", "marked": False,
                "error": "overrides must be a list"}
    overrides = [str(x) for x in overrides]

    if is_paused():
        return {"ok": False, "status": "blocked", "reason": "bot_paused",
                "marked": False, "error": "机器人已暂停，无法开始批量推送"}

    requested_ids = data.get("schedule_ids")
    if requested_ids is not None and not isinstance(requested_ids, list):
        return {"ok": False, "status": "invalid_request", "marked": False,
                "error": "schedule_ids must be a list"}

    due = _due_schedule_ids(requested_ids)
    if not due:
        return {"ok": False, "status": "empty", "marked": False,
                "error": "没有待跟进的客户"}

    # Check the first item with the unified policy. This catches the proactive
    # switch and quiet hours before marking the day; the per-item send-now
    # endpoint re-checks every customer immediately before sending.
    first = due[0]
    decision = check_proactive_eligibility(
        {"status": first.get("customer_status"), "phone": first.get("phone")},
        first, manual=True, overrides=overrides,
    )
    if not decision["allowed"]:
        blocker = decision["blockers"][0]
        return {"ok": False, "status": "blocked", "reason": blocker["code"],
                "marked": False, "error": blocker["message"],
                "blockers": decision["blockers"],
                "overridable_reasons": decision["overridable_reasons"],
                "overrides_applied": decision["overrides_applied"]}

    cfg = get_config()
    usage = get_daily_quota_usage()
    if usage["total"] >= cfg.business.max_auto_replies_per_day:
        return {"ok": False, "status": "blocked", "reason": "quota_exceeded",
                "marked": False, "error": "今日主动跟进配额已用完"}

    from bot_state import mark_batch_pushed_today, _today_str
    mark_batch_pushed_today()
    return {
        "ok": True, "status": "started", "marked": True, "date": _today_str(),
        "count": len(due), "schedule_ids": [s["id"] for s in due],
        "overrides_applied": decision["overrides_applied"],
    }


@router.post("/followups/mark-batch-pushed")
async def mark_batch_pushed(data: dict | None = None):
    """Compatibility endpoint.

    Kept for older clients, but now requires an explicit confirmation and a
    non-empty due queue. Prefer ``/followups/batch/start``.
    """
    return await start_batch_push(data)


@router.get("/followups/auto-batch-status")
async def auto_batch_status():
    """Return whether today's batch push was already done manually."""
    from bot_state import was_batch_pushed_today, _today_str
    cfg = get_config()
    usage = get_daily_quota_usage()
    return {
        "manual_pushed_today": was_batch_pushed_today(),
        "date": _today_str(),
        "proactive_followups_enabled": is_proactive_followups_enabled(),
        "daily_quota": {
            "limit": cfg.business.max_auto_replies_per_day,
            "used": usage["used"],
            "reserved": usage["reserved"],
            "total": usage["total"],
        },
    }

# ── Product Docs ──────────────────────────────────────────────────────────────

@router.get("/product-docs")
async def list_product_docs():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM product_docs ORDER BY updated_at DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/product-docs")
async def create_product_doc(data: dict):
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title:
        return {"error": "title required"}
    from database import db_write
    with db_write() as conn:
        cur = conn.execute(
            "INSERT INTO product_docs(title, content) VALUES(?,?)", (title, content))
        row = conn.execute("SELECT * FROM product_docs WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "doc": dict(row)}


@router.put("/product-docs/{doc_id}")
async def update_product_doc(doc_id: int, data: dict):
    from database import db_write
    with db_write() as conn:
        row = conn.execute("SELECT * FROM product_docs WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return {"error": "not found"}
        title = data.get("title", row["title"])
        content = data.get("content", row["content"])
        conn.execute(
            "UPDATE product_docs SET title=?, content=?, updated_at=? WHERE id=?",
            (title, content, datetime.now().isoformat(), doc_id))
        updated = conn.execute("SELECT * FROM product_docs WHERE id=?", (doc_id,)).fetchone()
    return {"ok": True, "doc": dict(updated)}


@router.delete("/product-docs/{doc_id}")
async def delete_product_doc(doc_id: int):
    from database import db_write
    with db_write() as conn:
        conn.execute("DELETE FROM product_docs WHERE id=?", (doc_id,))
    return {"ok": True}


_MAX_DOC_BYTES = 50 * 1024 * 1024


def _process_uploaded_docs_sync(file_items: list[tuple[str, bytes]]) -> dict:
    from database import db_write, save_product_file

    imported = []
    failed = []
    mime_map = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel",
        "csv": "text/csv",
        "txt": "text/plain", "md": "text/markdown",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp",
    }

    for filename, content_bytes in file_items:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        if len(content_bytes) > _MAX_DOC_BYTES:
            failed.append({"filename": filename, "reason": "文件过大（最大 50MB）"})
            continue

        try:
            content = ""
            if ext in ("txt", "md", "markdown"):
                content = content_bytes.decode("utf-8-sig")
            elif ext == "pdf":
                from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(content_bytes))
                if reader.is_encrypted:
                    try:
                        reader.decrypt("")
                    except Exception:
                        failed.append({"filename": filename, "reason": "PDF 已加密且无法解密，请提供未加密的版本"})
                        continue
                parts = []
                for page in reader.pages:
                    t = page.extract_text()
                    if t:
                        parts.append(t)
                content = "\n\n".join(parts)
                if not content.strip():
                    try:
                        import fitz
                        ocr = _get_ocr()
                        doc = fitz.open(stream=content_bytes, filetype="pdf")
                        ocr_parts = []
                        for page in doc:
                            pix = page.get_pixmap(dpi=150)
                            img_bytes = pix.tobytes("png")
                            out = ocr(img_bytes)
                            if out.txts:
                                lines = [t.strip() for t in out.txts if t and str(t).strip()]
                                if lines:
                                    ocr_parts.append("\n".join(lines))
                        doc.close()
                        content = "\n\n".join(ocr_parts)
                    except Exception:
                        pass
            elif ext == "docx":
                from docx import Document
                doc = Document(io.BytesIO(content_bytes))
                lines = []
                for p in doc.paragraphs:
                    if p.text.strip():
                        lines.append(p.text)
                for table in doc.tables:
                    for row in table.rows:
                        row_text = " | ".join(cell.text for cell in row.cells)
                        if row_text.strip():
                            lines.append(row_text)
                content = "\n".join(lines)
                if not content.strip():
                    try:
                        ocr = _get_ocr()
                        ocr_lines = []
                        for rel in doc.part.rels.values():
                            if "image" in rel.reltype:
                                try:
                                    img_bytes = rel.target_part.blob
                                    out = ocr(img_bytes)
                                    if out.txts:
                                        for t in out.txts:
                                            if t and str(t).strip():
                                                ocr_lines.append(str(t).strip())
                                except Exception:
                                    pass
                        content = "\n".join(ocr_lines)
                    except Exception:
                        pass
            elif ext in ("png", "jpg", "jpeg", "gif", "bmp", "webp"):
                try:
                    ocr = _get_ocr()
                    out = ocr(content_bytes)
                    lines = [str(t).strip() for t in out.txts if t and str(t).strip()]
                    content = "\n".join(lines)
                except Exception as e:
                    failed.append({"filename": filename, "reason": f"图片 OCR 识别失败: {str(e)}"})
                    continue
            elif ext == "xlsx":
                wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True)
                parts = []
                for name in wb.sheetnames:
                    ws = wb[name]
                    rows = list(ws.iter_rows(values_only=True))
                    if not rows:
                        continue
                    parts.append(f"## {name}")
                    for row in rows:
                        parts.append(" | ".join(str(c or "") for c in row))
                wb.close()
                content = "\n".join(parts)
            elif ext == "xls":
                import xlrd
                wb = xlrd.open_workbook(file_contents=content_bytes)
                parts = []
                for name in wb.sheet_names():
                    ws = wb.sheet_by_name(name)
                    if ws.nrows == 0:
                        continue
                    parts.append(f"## {name}")
                    for r in range(ws.nrows):
                        row_vals = [str(ws.cell_value(r, c) or "") for c in range(ws.ncols)]
                        parts.append(" | ".join(row_vals))
                content = "\n".join(parts)
            elif ext == "csv":
                text = content_bytes.decode("utf-8-sig")
                reader = csv.reader(io.StringIO(text))
                content = "\n".join(" | ".join(row) for row in reader)
            else:
                failed.append({"filename": filename, "reason": f"不支持的文件类型 .{ext}"})
                continue

            if not content.strip():
                failed.append({"filename": filename, "reason": "无法提取文本内容，文件可能为扫描版或空白"})
                continue

            with db_write() as conn:
                cur = conn.execute(
                    "INSERT INTO product_docs(title, content) VALUES(?,?)",
                    (title, content[:200000]))
                doc_id = cur.lastrowid
            mime_type = mime_map.get(ext, "application/octet-stream")
            save_product_file(doc_id, filename, mime_type, content_bytes)
            imported.append({"id": doc_id, "title": title})
        except Exception as e:
            failed.append({"filename": filename, "reason": f"解析失败: {str(e)}"})

    return {"imported": imported, "failed": failed}


@router.post("/product-docs/upload")
async def upload_product_docs(files: list[UploadFile] = File(...)):
    file_items: list[tuple[str, bytes]] = []
    for file in files:
        filename = file.filename or "untitled"
        content_bytes = await file.read()
        file_items.append((filename, content_bytes))
    return await asyncio.to_thread(_process_uploaded_docs_sync, file_items)


# ── QR Code ────────────────────────────────────────────────────────────────────

@router.get("/qr")
async def get_qr():
    qr = get_pending_qr()
    if qr:
        return {"status": "pending", "qr_text": qr}
    return {"status": "none"}


@router.post("/qr/clear")
async def clear_qr():
    clear_pending_qr()
    return {"ok": True}


# ── Bridge ─────────────────────────────────────────────────────────────────────

def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """Read up to the last ``max_lines`` lines without loading huge log files completely."""
    from collections import deque
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return [line.rstrip("\n") for line in deque(fh, maxlen=max_lines)]


@router.post("/bridge/start")
async def bridge_start():
    """为当前租户拉起一个独立的 bridge 进程（已运行则复用）。"""
    import bridge_manager
    import tenancy

    tenant = tenancy.current()
    if tenant is None:
        return {"ok": False, "error": "需要登录"}
    try:
        result = await asyncio.to_thread(bridge_manager.start, tenant)
    except Exception as exc:
        logger.warning("bridge 启动失败: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}


@router.post("/bridge/stop")
async def bridge_stop():
    import bridge_manager
    import tenancy

    tenant = tenancy.current()
    if tenant is None:
        return {"ok": False, "error": "需要登录"}
    await asyncio.to_thread(bridge_manager.stop, tenant.user_id)
    return {"ok": True}


@router.post("/bridge/restart")
async def bridge_restart():
    """Atomically restart the current tenant's bridge under bridge_manager lock."""
    import bridge_manager
    import tenancy

    tenant = tenancy.current()
    if tenant is None:
        return {"ok": False, "error": "需要登录"}
    try:
        result = await asyncio.to_thread(bridge_manager.restart, tenant)
    except Exception as exc:
        logger.warning("bridge 重启失败: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}


def _read_bridge_events_sync(paths: list[Path]) -> list[str]:
    bridge_events = []
    try:
        for path in paths:
            for line in _tail_lines(path, 400):
                if "Bridge not started" in line:
                    continue
                if any(k in line for k in ("WHATSAPP_READY", "WHATSAPP_DISCONNECTED",
                                           "Bridge", "bridge", "[WARNING]", "[ERROR]")):
                    bridge_events.append(line)
        bridge_events.sort(key=lambda line: line[:19])
    except Exception:
        pass
    return bridge_events[-20:]


@router.get("/bridge")
async def bridge_status():
    status = await _get_bridge_status()
    status["qr_pending"] = get_pending_qr() is not None
    paths = _activity_log_candidates()
    bridge_events = await asyncio.to_thread(_read_bridge_events_sync, paths)
    return {
        **status,
        "recent_events": bridge_events,
    }


# ── Proxy Config ──────────────────────────────────────────────────────────────

@router.get("/netcheck")
async def netcheck():
    """出站网络自检：直连出口、代理出口、bridge 状态。

    排查"消息发不出去"时先看这里——能区分是代理没生效、代理挂了，
    还是 WhatsApp 本身拒绝了这个 IP。
    """
    import httpx

    import bridge_manager
    import tenancy

    tenant = tenancy.current()
    proxy_url = bridge_manager.proxy_for(tenant)

    async def fetch_ip(proxy: str | None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=6.0, trust_env=False,
                                         proxy=proxy) as client:
                resp = await client.get("https://ipinfo.io/json")
                resp.raise_for_status()
                data = resp.json()
                return {"ok": True, "ip": data.get("ip"),
                        "country": data.get("country"), "org": data.get("org")}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    result = {
        "proxy": proxy_url or None,
        "bridge": bridge_manager.status(tenant),
        "direct": await fetch_ip(None),
    }
    if proxy_url:
        result["via_proxy"] = await fetch_ip(proxy_url)
    return result


@router.get("/proxy")
async def get_proxy():
    cfg = get_config()
    import yaml
    data_dir = get_data_dir()
    config_path = data_dir / "config.yaml"
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return {"proxy": raw.get("bridge", {}).get("proxy", "")}
    return {"proxy": cfg.bridge.proxy}


@router.post("/proxy")
async def set_proxy(data: dict):
    proxy_url = (data.get("proxy") or "").strip()
    import yaml
    data_dir = get_data_dir()
    config_path = data_dir / "config.yaml"
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        raw = {}
    raw.setdefault("bridge", {})["proxy"] = proxy_url
    config_path.write_text(yaml.dump(raw, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    # Reload only the current tenant/global config cache without leaking into global _config.
    from config import reload_current_config
    reload_current_config(config_path)

    # Restart running tenant bridge so the updated proxy env takes effect immediately.
    import bridge_manager
    import tenancy
    tenant = tenancy.current()
    restarted = False
    restart_error = None
    if tenant is not None and bridge_manager.status(tenant).get("running"):
        try:
            await asyncio.to_thread(bridge_manager.restart, tenant)
            restarted = True
        except Exception as exc:
            restart_error = str(exc)
            logger.warning("代理更新后重启 bridge 失败: %s", exc)

    note = (
        "代理已保存并已重启 Bridge 使其生效。" if restarted
        else (f"代理已保存，但重启 Bridge 失败：{restart_error}" if restart_error
              else "代理已保存，启动 Bridge 后将自动生效。")
    )
    return {"ok": True, "proxy": proxy_url, "restarted": restarted, "note": note}


# ── Bot Pause/Resume ───────────────────────────────────────────────────────────

@router.get("/settings/proactive-followups")
async def get_proactive_followups_setting():
    """Backend source of truth for the user-facing proactive follow-up switch."""
    cfg = get_config()
    usage = get_daily_quota_usage()
    return {
        "enabled": is_proactive_followups_enabled(),
        "configured": is_proactive_followups_configured(),
        "scope": "automatic_proactive_followups",
        "applies_to": ["followup_check_5m", "auto_batch_10am"],
        "does_not_apply_to": ["instant_inbound_reply", "manual_chat_send"],
        "daily_quota": {
            "limit": cfg.business.max_auto_replies_per_day,
            "used": usage["used"],
            "reserved": usage["reserved"],
            "total": usage["total"],
        },
    }


@router.put("/settings/proactive-followups")
async def update_proactive_followups_setting(data: dict):
    if "enabled" not in data or not isinstance(data.get("enabled"), bool):
        return {"ok": False, "error": "enabled (boolean) required"}
    set_proactive_followups_enabled(data["enabled"])
    logger.info("Proactive follow-ups switched %s", "on" if data["enabled"] else "off")
    cfg = get_config()
    usage = get_daily_quota_usage()
    return {
        "ok": True,
        "enabled": data["enabled"],
        "configured": True,
        "scope": "automatic_proactive_followups",
        "applies_to": ["followup_check_5m", "auto_batch_10am"],
        "does_not_apply_to": ["instant_inbound_reply", "manual_chat_send"],
        "daily_quota": {
            "limit": cfg.business.max_auto_replies_per_day,
            "used": usage["used"],
            "reserved": usage["reserved"],
            "total": usage["total"],
        },
    }


@router.get("/bot-state")
async def get_bot_state():
    dashboard_conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    today_replies = dashboard_conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE ai_generated=1 AND direction='outbound' "
        "AND status='sent' AND date(sent_at)=?",
        (today,)
    ).fetchone()[0]
    today_followups = dashboard_conn.execute(
        "SELECT COUNT(*) FROM sent_followups WHERE status='sent' AND date(sent_at)=?",
        (today,)
    ).fetchone()[0]
    usage = get_daily_quota_usage()
    return {
        "paused": is_paused(),
        "proactive_followups_enabled": is_proactive_followups_enabled(),
        "proactive_followups_configured": is_proactive_followups_configured(),
        "today_ai_replies": today_replies,
        "today_followups": today_followups,
        "daily_quota": {
            "used": usage["used"], "reserved": usage["reserved"],
            "total": usage["total"],
            "limit": get_config().business.max_auto_replies_per_day,
        },
    }


@router.post("/bot-state/toggle")
async def toggle_bot_state():
    new_state = toggle_paused()
    return {"paused": new_state, "status": "paused" if new_state else "running"}


async def _get_bridge_status() -> dict:
    cfg = get_config()
    try:
        import bridge_manager
        from bridge_client import _shared_http_client

        port = bridge_manager.current_port()
        headers = bridge_manager.auth_headers()
        client = _shared_http_client()
        resp = await client.get(
            f"http://{cfg.bridge.host}:{port}/status",
            headers=headers,
            timeout=3.0,
        )
        resp.raise_for_status()
        bs = resp.json()
        if not isinstance(bs, dict) or not isinstance(bs.get("authenticated"), bool):
            raise ValueError("Bridge returned an invalid status response")
        return {
            "online": True,
            "authenticated": bs.get("authenticated", False),
            "phone": bs.get("phone"),
        }
    except Exception as e:
        return {"online": False, "authenticated": False, "phone": None, "error": str(e)}


# ── Agent Monitoring ──────────────────────────────────────────────────────────

@router.get("/agent/status")
async def agent_status():
    """Get agent activity summary: decisions, segment distribution, scores."""
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")

    # Segment distribution
    segments = conn.execute(
        "SELECT segment, COUNT(*) as cnt FROM lead_scores GROUP BY segment"
    ).fetchall()
    segment_counts = {r["segment"]: r["cnt"] for r in segments}
    total_scored = sum(segment_counts.values())

    # Today's decisions
    today_decisions = conn.execute(
        "SELECT COUNT(*) as cnt FROM agent_decisions WHERE date(created_at)=?",
        (today,)
    ).fetchone()["cnt"]

    # Recent decisions (last 20)
    recent = conn.execute(
        "SELECT ad.*, c.name, c.phone FROM agent_decisions ad "
        "LEFT JOIN customers c ON ad.customer_id = c.id "
        "ORDER BY ad.created_at DESC LIMIT 20"
    ).fetchall()

    # Top hot leads
    hot_leads = conn.execute(
        "SELECT ls.*, c.name, c.phone, c.company FROM lead_scores ls "
        "JOIN customers c ON ls.customer_id = c.id "
        "WHERE ls.segment = 'hot' ORDER BY ls.score DESC LIMIT 10"
    ).fetchall()

    # Memory entries count
    memory_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM ai_memory_entries"
    ).fetchone()["cnt"]

    return {
        "total_scored": total_scored,
        "segment_counts": segment_counts,
        "today_decisions": today_decisions,
        "total_memories": memory_count,
        "hot_leads": [{"id": r["customer_id"], "name": r["name"],
                        "phone": r["phone"], "score": r["score"],
                        "signals": r["signals"]} for r in hot_leads],
        "recent_decisions": [
            {"id": r["id"], "customer_id": r["customer_id"],
             "customer_name": r["name"], "decision_type": r["decision_type"],
             "reasoning": r["reasoning"], "created_at": r["created_at"]}
            for r in recent
        ],
    }


@router.get("/agent/decisions")
async def agent_decisions(limit: int = Query(50, ge=10, le=200)):
    """Get recent agent decision logs."""
    from database import get_recent_decisions
    return {"decisions": get_recent_decisions(limit)}


@router.get("/agent/customer-analysis/{customer_id}")
async def agent_customer_analysis(customer_id: int):
    """Full analysis for a single customer: score, segment, intents, memory."""
    from scoring_engine import score_customer
    from intent_analyzer import analyze_conversation_intent
    from database import get_customer_memory, get_customer_decisions

    def _compute():
        score_data = score_customer(customer_id)
        intent_data = analyze_conversation_intent(customer_id)
        memory = get_customer_memory(customer_id, limit=10)
        decisions = get_customer_decisions(customer_id, limit=20)
        customer = get_connection().execute(
            "SELECT * FROM customers WHERE id=?", (customer_id,)
        ).fetchone()
        return {
            "customer": dict(customer) if customer else None,
            "computed_at": datetime.now().isoformat(),
            "score": score_data,
            "intent": intent_data,
            "memory": [dict(m) for m in memory],
            "decisions": [dict(d) for d in decisions],
        }

    return await asyncio.to_thread(_compute)


@router.post("/agent/run-scoring")
async def trigger_scoring():
    """Manually trigger a full re-scoring of all customers."""
    from scoring_engine import score_all_customers
    result = await asyncio.to_thread(score_all_customers)
    return {"ok": True, "result": result}


@router.post("/agent/run-strategy-eval")
async def trigger_strategy_eval():
    """Manually trigger strategy evaluation for all customers."""
    from strategy_manager import evaluate_all_strategies
    result = await asyncio.to_thread(evaluate_all_strategies)
    return {"ok": True, "result": result}


# ── Activity Log ───────────────────────────────────────────────────────────────

def _activity_log_candidates() -> list[Path]:
    """Return existing log files, newest first.

    The desktop launcher writes ``<userData>/logs/backend.log``; the standalone
    web runner writes ``web.log``; ``main.py`` writes ``logs/bot.log`` relative
    to cwd. The old endpoint only looked at repo/logs/bot.log, so the UI could
    show an empty "运行日志" page precisely when diagnostics were needed.
    """
    data_dir = get_data_dir()
    candidates = [
        data_dir / "logs" / "bridge.log",
        data_dir / "logs" / "backend.log",
        data_dir / "logs" / "web.log",
        data_dir / "logs" / "bot.log",
        Path(__file__).resolve().parent.parent / "logs" / "bot.log",
    ]
    existing: list[tuple[float, Path]] = []
    for candidate in candidates:
        try:
            if candidate.is_file():
                existing.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    return [path for _, path in sorted(existing, reverse=True)]


@router.get("/activity")
async def activity_log(lines: int = Query(100, ge=10, le=500)):
    paths = _activity_log_candidates()
    if not paths:
        return {"lines": [], "source": None,
                "error": "未找到日志文件（backend.log/web.log/bot.log）"}

    def _read_and_filter() -> list[str]:
        log_lines = []
        max_raw = min(max(lines * 20, 1000), 10000)
        for log_path in paths:
            log_lines.extend(_tail_lines(log_path, max_raw))
        filtered = [l for l in log_lines
                    if ("[DEBUG]" not in l
                        and ("[INFO]" in l or "[ERROR]" in l or "[CRITICAL]" in l or "[WARNING]" in l))]
        in_qr = False
        clean = []
        for l in filtered:
            if ">>> QR_RECEIVED <<<" in l:
                in_qr = True
                clean.append(l)
                continue
            if ">>> QR_END <<<" in l:
                in_qr = False
                clean.append(l)
                continue
            if not in_qr:
                clean.append(l)
        clean.sort(key=lambda line: line[:19])
        return clean[-lines:]

    try:
        result_lines = await asyncio.to_thread(_read_and_filter)
        return {"lines": result_lines, "source": ", ".join(str(p) for p in paths)}
    except Exception as e:
        return {"lines": [], "source": ", ".join(str(p) for p in paths), "error": str(e)}


# ── Orders ─────────────────────────────────────────────────────────────────────

@router.get("/orders")
async def orders_list(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
    search: str = Query(""),
    status: str = Query(""),
    sort: str = Query("created_at"),
    order: str = Query("desc"),
):
    conn = get_connection()
    allowed_sorts = {"order_no", "product", "quantity", "total_amount", "status", "order_date", "created_at"}
    if sort not in allowed_sorts:
        sort = "created_at"
    if order not in ("asc", "desc"):
        order = "desc"

    where = ["1=1"]
    params = []
    if search:
        where.append("(o.order_no LIKE ? OR o.product LIKE ? OR c.name LIKE ? OR c.phone LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s, s])
    if status:
        where.append("o.status = ?")
        params.append(status)

    where_clause = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id=c.id WHERE {where_clause}",
        params
    ).fetchone()[0]

    params_with_limit = params + [per_page, (page - 1) * per_page]
    rows = conn.execute(
        f"""SELECT o.*, c.name as customer_name, c.phone as customer_phone, c.company as customer_company
            FROM orders o JOIN customers c ON o.customer_id=c.id
            WHERE {where_clause}
            ORDER BY o.{sort} {order} LIMIT ? OFFSET ?""",
        params_with_limit
    ).fetchall()

    return {
        "total": total, "page": page, "per_page": per_page,
        "items": [dict(r) for r in rows],
    }


@router.get("/orders/{order_id}")
async def order_detail(order_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT o.*, c.name as customer_name, c.phone as customer_phone, c.company as customer_company "
        "FROM orders o JOIN customers c ON o.customer_id=c.id WHERE o.id=?",
        (order_id,)
    ).fetchone()
    if not row:
        return {"error": "not found"}
    return {"order": dict(row)}


@router.post("/orders")
async def create_order(data: dict):
    customer_id = data.get("customer_id")
    if not customer_id:
        return {"error": "customer_id required"}
    return await asyncio.to_thread(create_order_record, int(customer_id), data)


@router.put("/orders/{order_id}")
async def update_order(order_id: int, data: dict):
    return await asyncio.to_thread(update_order_record, order_id, data)


@router.delete("/orders/{order_id}")
async def delete_order(order_id: int):
    deleted = await asyncio.to_thread(delete_order_record, order_id)
    if not deleted:
        return {"error": "not found"}
    return {"ok": True}


@router.get("/customers/{customer_id}/orders")
async def customer_orders(customer_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE customer_id=? ORDER BY created_at DESC",
        (customer_id,)
    ).fetchall()
    return [dict(r) for r in rows]
