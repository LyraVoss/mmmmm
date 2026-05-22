"""
AgentMail Utility — specialized email service for the Agent Universe.
Used by Mymm to send autonomous code edits and architectural suggestions to the owner.
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bot.utils.logger import get_logger
import os

log = get_logger(__name__)

# Configuration
RECIPIENT_EMAIL = "alexgudde0@gmail.com"
AGENTMAIL_USER = os.environ.get("AGENTMAIL_USER", "mymm@agentmail.ai")
AGENTMAIL_PASS = os.environ.get("AGENTMAIL_PASS", "") # Set in Railway

async def send_agent_email(subject: str, body: str):
    """Send an autonomous email from Mymm to the Owner."""
    if not AGENTMAIL_PASS:
        log.warning("AgentMail skipped: AGENTMAIL_PASS not set in environment.")
        return

    msg = MIMEMultipart()
    msg['From'] = f"Mymm AI Agent <{AGENTMAIL_USER}>"
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"Mymm Intel: {subject}"

    msg.attach(MIMEText(body, 'plain'))

    try:
        # Using standard SMTP for AgentMail compatibility
        with smtplib.SMTP_SSL("smtp.agentmail.ai", 465) as server:
            server.login(AGENTMAIL_USER, AGENTMAIL_PASS)
            server.sendmail(AGENTMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
        log.info("📧 Code refinements emailed to %s", RECIPIENT_EMAIL)
    except Exception as e:
        log.error("Failed to send AgentMail: %s", e)