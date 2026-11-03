"""Django email backend that sends through Microsoft Graph instead of SMTP.

Every Django email (crash reports via mail_admins, password resets, later the
customer notifications) goes out from settings.M365_SENDER.
"""

import logging

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

from . import graph

logger = logging.getLogger(__name__)


class GraphEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            body, html = message.body, False
            for content, mimetype in getattr(message, "alternatives", []) or []:
                if mimetype == "text/html":
                    body, html = content, True
                    break
            try:
                graph.send_mail(
                    sender=settings.M365_SENDER,
                    to=list(message.to),
                    cc=list(message.cc),
                    reply_to=list(message.reply_to),
                    subject=message.subject,
                    body=body,
                    html=html,
                )
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
                # A warning sits below the crash-email level (ERROR), so a mail
                # outage cannot set off a loop of crash emails about itself.
                logger.warning("Graph sendMail failed", exc_info=True)
        return sent
