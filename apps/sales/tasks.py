from procrastinate.contrib.django import app


# The website's contact form emails: every 5 minutes, when set up.
@app.periodic(cron="*/5 * * * *")
@app.task(queue="default")
def check_webform_mail(timestamp: int) -> None:
    from . import webform

    if webform.enabled():
        webform.check_mail()
