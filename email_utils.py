"""
Email notification utility for SHP TEAM.

Sends transactional notification emails (new leads / bookings) to the admin
inbox using SMTP (Gmail-compatible). Fully env-driven and fails gracefully:
if SMTP is not configured, calls become no-ops (logged) so the app never
breaks when credentials are absent.

Required environment variables to ACTIVATE sending:
  SMTP_USER      - sending Gmail address (e.g. notifications@shpteam.in)
  SMTP_PASSWORD  - Gmail App Password (16 chars, NOT the account password)
Optional:
  SMTP_HOST      - default "smtp.gmail.com"
  SMTP_PORT      - default 587 (STARTTLS)
  SMTP_FROM      - default SMTP_USER
  NOTIFY_EMAIL   - recipient; default ADMIN_EMAIL
"""
import os
import ssl
import smtplib
import asyncio
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger("email")


def _smtp_config():
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not user or not password:
        return None
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "from_addr": os.environ.get("SMTP_FROM", user).strip() or user,
        "to_addr": (os.environ.get("NOTIFY_EMAIL")
                    or os.environ.get("ADMIN_EMAIL")
                    or user).strip(),
    }


def _send_sync(subject: str, html_body: str, to_addr: str = None) -> bool:
    cfg = _smtp_config()
    if not cfg:
        logger.info("SMTP not configured; skipping email '%s'", subject)
        return False
    recipient = to_addr or cfg["to_addr"]
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"SHP TEAM <{cfg['from_addr']}>"
        msg["To"] = recipient
        msg.attach(MIMEText(html_body, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as server:
            server.starttls(context=context)
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["from_addr"], [recipient], msg.as_string())
        logger.info("Email sent: '%s' -> %s", subject, recipient)
        return True
    except Exception as e:  # never let email failure break the request
        logger.error("Failed to send email '%s': %s", subject, e)
        return False


async def send_notification_email(subject: str, html_body: str) -> None:
    """Fire-and-forget send to the admin NOTIFY_EMAIL (non-blocking)."""
    try:
        await asyncio.to_thread(_send_sync, subject, html_body)
    except Exception as e:
        logger.error("Email dispatch error '%s': %s", subject, e)


async def send_email_to(to_addr: str, subject: str, html_body: str) -> None:
    """Fire-and-forget send to a specific recipient (non-blocking)."""
    try:
        await asyncio.to_thread(_send_sync, subject, html_body, to_addr)
    except Exception as e:
        logger.error("Email dispatch error '%s': %s", subject, e)


def build_reset_email(name: str, reset_link: str) -> tuple:
    rows = (
        f'<tr><td style="padding:6px 0;color:#0f172a;">Hi {name or "there"},</td></tr>'
        f'<tr><td style="padding:6px 0;color:#475569;">We received a request to reset your SHP TEAM password. '
        f'Click the button below to choose a new password. This link expires in 1 hour.</td></tr>'
        f'<tr><td style="padding:18px 0;">'
        f'<a href="{reset_link}" style="background:#D4AF37;color:#0f172a;font-weight:700;text-decoration:none;'
        f'padding:12px 22px;border-radius:9999px;display:inline-block;">Reset Password</a></td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;font-size:12px;">'
        f'If you did not request this, you can safely ignore this email.</td></tr>'
    )
    return "Reset your SHP TEAM password", _wrap("Password Reset Request", rows)


def _row(label: str, value) -> str:
    if value in (None, ""):
        return ""
    return (
        f'<tr><td style="padding:6px 12px;color:#64748b;font-weight:600;">{label}</td>'
        f'<td style="padding:6px 12px;color:#0f172a;">{value}</td></tr>'
    )


def _wrap(heading: str, rows_html: str) -> str:
    return f"""\
<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:auto;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
  <div style="background:#0F172A;padding:18px 20px;">
    <span style="color:#D4AF37;font-weight:800;font-size:18px;">SHP TEAM</span>
    <div style="color:#cbd5e1;font-size:12px;letter-spacing:2px;">BUILD · DESIGN · TRUST</div>
  </div>
  <div style="padding:20px;">
    <h2 style="margin:0 0 14px;color:#0f172a;font-size:18px;">{heading}</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">{rows_html}</table>
    <p style="margin-top:18px;color:#94a3b8;font-size:12px;">This is an automated notification from your SHP TEAM website.</p>
  </div>
</div>"""


def build_lead_email(lead: dict) -> tuple:
    rows = (
        _row("Name", lead.get("name"))
        + _row("Email", lead.get("email"))
        + _row("Phone", lead.get("phone"))
        + _row("City", lead.get("city"))
        + _row("Service", lead.get("service"))
        + _row("Message", lead.get("message"))
    )
    subject = f"New Inquiry: {lead.get('name', 'Website Lead')} ({lead.get('service', 'General')})"
    return subject, _wrap("New Website Inquiry", rows)


def build_booking_email(booking: dict) -> tuple:
    is_site = booking.get("booking_type") == "site_visit"
    heading = "New Site Visit Booking" if is_site else "New Consultation Booking"
    rows = (
        _row("Name", booking.get("name"))
        + _row("Email", booking.get("email"))
        + _row("Phone", booking.get("phone"))
        + _row("Type", booking.get("package_label") or booking.get("consultation_type"))
        + _row("Service Interest", booking.get("service_interest"))
        + _row("Date", booking.get("date"))
        + _row("Time", booking.get("time"))
        + _row("Address", booking.get("address"))
        + _row("City", booking.get("city"))
        + _row("Pincode", booking.get("pincode"))
        + _row("Amount", f"₹{booking.get('amount')}" if booking.get("amount") else None)
        + _row("Message", booking.get("message"))
    )
    subject = f"{heading}: {booking.get('name', 'Customer')}"
    return subject, _wrap(heading, rows)


def build_unlock_payment_email(unlock: dict) -> tuple:
    """Alert email sent to admin when a contractor pays to unlock an opportunity."""
    rows = (
        _row("💰 Amount Paid",       f"₹{unlock.get('amount', 49)}")
        + _row("Razorpay Payment ID", unlock.get("razorpay_payment_id"))
        + _row("Razorpay Order ID",   unlock.get("razorpay_order_id"))
        + _row("Opportunity",         unlock.get("opportunity_title"))
        + _row("Opportunity ID",      unlock.get("opportunity_id"))
        + _row("Contractor Email",    unlock.get("contractor_email"))
        + _row("Contractor Name",     unlock.get("contractor_name"))
        + _row("Paid At",             unlock.get("unlocked_at"))
    )
    subject = f"💰 Payment Received ₹{unlock.get('amount', 49)} — Opportunity Unlocked by {unlock.get('contractor_email', 'Contractor')}"
    return subject, _wrap("New Opportunity Unlock Payment", rows)


def build_assign_id_email(data: dict) -> tuple:
    """Sent to contractor after paying — gives Assign ID + project details. No client info."""
    assign_id = data.get("assign_id", "N/A")
    contractor_name = data.get("contractor_name", "Contractor")
    opp_title = data.get("opportunity_title", "Opportunity")
    opp_type = (data.get("opportunity_type") or "").replace("_", " ").title()
    city = data.get("city", "")
    budget = data.get("estimated_budget")
    duration = data.get("estimated_duration", "")
    budget_row = _row("Estimated Budget", f"₹{int(budget):,}") if budget else ""

    rows = (
        f'<tr><td colspan="2" style="padding:8px 0;">'
        f'<div style="background:#0f172a;border-radius:12px;padding:20px;text-align:center;">'
        f'<div style="font-size:11px;color:#94a3b8;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px;">Your Assign ID</div>'
        f'<div style="font-size:36px;font-weight:900;color:#D4AF37;font-family:monospace;letter-spacing:6px;">{assign_id}</div>'
        f'<div style="font-size:11px;color:#64748b;margin-top:4px;">Track your status at shpteam.in/track</div>'
        f'</div></td></tr>'
        + f'<tr><td colspan="2" style="padding:10px 0 6px;color:#475569;">Hi <strong>{contractor_name}</strong>, your application is confirmed! Here are the project details:</td></tr>'
        + _row("Project", opp_title)
        + _row("Type", opp_type)
        + _row("City", city)
        + _row("Duration", duration)
        + budget_row
        + f'<tr><td colspan="2" style="padding:14px 0 4px;">'
        f'<div style="background:#fefce8;border:1px solid #fde047;border-radius:8px;padding:14px;">'
        f'<strong style="color:#854d0e;">Next Step:</strong> <span style="color:#713f12;">Submit your commission offer to boost your chances of being selected.</span>'
        f'</div></td></tr>'
        + f'<tr><td colspan="2" style="padding:14px 0 4px;text-align:center;">'
        f'<a href="https://shpteam.in/work-opportunities" style="background:#D4AF37;color:#0f172a;font-weight:700;'
        f'text-decoration:none;padding:12px 28px;border-radius:9999px;display:inline-block;">Submit Commission Offer →</a></td></tr>'
        + f'<tr><td colspan="2" style="padding:10px 0 4px;text-align:center;font-size:12px;color:#94a3b8;">'
        f'Track your status: <a href="https://shpteam.in/track" style="color:#D4AF37;">shpteam.in/track</a> using ID <strong style="font-family:monospace;">{assign_id}</strong></td></tr>'
    )
    subject = f"[SHP TEAM] Application Confirmed! Assign ID: {assign_id}"
    return subject, _wrap("Application Confirmed 🎉", rows)


def build_commission_offer_email(data: dict) -> tuple:
    """Sent to ADMIN when a contractor submits a commission offer."""
    commission_display = (
        f"{data.get('commission_value')}%" if data.get('commission_type') == 'percent'
        else f"₹{data.get('commission_value', 0):,.0f}"
    )
    rows = (
        _row("💰 Commission Offer", commission_display)
        + _row("Contractor", data.get("contractor_name"))
        + _row("Contractor Email", data.get("contractor_email"))
        + _row("Opportunity", data.get("opportunity_title"))
        + _row("Assign ID", data.get("assign_id"))
        + _row("Note", data.get("note") or "—")
    )
    subject = f"💼 New Commission Offer — {commission_display} from {data.get('contractor_name', 'Contractor')}"
    return subject, _wrap("New Commission Offer Received", rows)


def build_rejection_wallet_credit_email(data: dict) -> tuple:
    """Sent to rejected contractors — informs them and confirms wallet credit."""
    contractor_name = data.get("contractor_name", "Contractor")
    opp_title = data.get("opportunity_title", "the opportunity")
    refund = data.get("refund_amount", 49)
    new_balance = data.get("new_wallet_balance", 0)
    assign_id = data.get("assign_id", "")

    rows = (
        f'<tr><td colspan="2" style="padding:8px 0 12px;color:#475569;">Hi <strong>{contractor_name}</strong>,<br><br>'
        f'Thank you for applying to <strong>{opp_title}</strong>. After careful review, we have selected another contractor for this project.<br><br>'
        f'We truly appreciate your interest and hope to work with you soon!</td></tr>'
        + f'<tr><td colspan="2" style="padding:4px 0 8px;">'
        f'<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:14px;">'
        f'<strong style="color:#166534;">✅ Wallet Credit:</strong> '
        f'<span style="color:#15803d;">₹{refund} has been credited to your SHP wallet. Your new balance is <strong>₹{new_balance}</strong>.</span>'
        f'<br><span style="font-size:12px;color:#4ade80;">Use this credit on your next application — it applies automatically!</span>'
        f'</div></td></tr>'
        + _row("Assign ID", assign_id)
        + f'<tr><td colspan="2" style="padding:14px 0 4px;text-align:center;">'
        f'<a href="https://shpteam.in/work-opportunities" style="background:#0f172a;color:#D4AF37;font-weight:700;'
        f'text-decoration:none;padding:12px 28px;border-radius:9999px;display:inline-block;">Browse Other Opportunities →</a></td></tr>'
    )
    subject = f"[SHP TEAM] Application Update + ₹{refund} Wallet Credit — {opp_title}"
    return subject, _wrap("Application Result + Wallet Credit", rows)
