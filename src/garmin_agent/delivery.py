"""Rendering and SMTP; an uncertain send is deliberately never retried automatically."""

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .analysis import display_metric

TEMPLATES = Path(__file__).parent / "templates"
environment = Environment(
    loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "xml"])
)


class DeliveryUncertain(RuntimeError):
    pass


def render(report):
    view = {
        **report,
        "metrics": [
            display_metric(m, report["preferences"]["units"]) for m in report["day"]["metrics"]
        ],
    }
    return (
        environment.get_template("report.html").render(**view),
        environment.get_template("report.txt").render(**view),
    )


class Mailer:
    def __init__(self, preferences, secrets):
        self.prefs = preferences
        self.password = secrets.get("smtp")

    def connect(self):
        cfg = self.prefs.smtp
        if not cfg.host or not cfg.username or not self.password:
            raise RuntimeError("Configure SMTP host, username and password in Connections.")
        client = None
        try:
            context = ssl.create_default_context()
            if cfg.security == "ssl":
                client = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=context)
            else:
                client = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(cfg.username, self.password)
            return client
        except Exception:
            if client:
                client.close()
            raise RuntimeError(
                "SMTP connection failed. Verify host, TLS mode and app password."
            ) from None

    def test(self):
        client = self.connect()
        self.close(client)

    @staticmethod
    def close(client):
        try:
            client.quit()
        except Exception:
            client.close()

    def send(self, run_id, report, html, text):
        if not self.prefs.recipient:
            raise RuntimeError("Set a report recipient in Preferences.")
        message = EmailMessage()
        message["From"] = str(self.prefs.smtp.sender or self.prefs.smtp.username)
        message["To"] = str(self.prefs.recipient)
        message["Subject"] = f"Your Garmin morning report · {report['day']['report_date']}"
        message["Message-ID"] = f"<{run_id}@garmin-morning.local>"
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        client = self.connect()
        try:
            client.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise RuntimeError(
                "SMTP rejected the email. Verify recipient and sender settings."
            ) from None
        except Exception:
            raise DeliveryUncertain(
                "The SMTP send result is unknown. Check your mailbox before "
                "taking further action; automatic resend is disabled."
            ) from None
        finally:
            self.close(client)
