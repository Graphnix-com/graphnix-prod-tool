from procrastinate.contrib.django import app


# 11:00 UTC = 04:00 in Alberta in winter: close jobs shipped long enough ago.
@app.periodic(cron="7 11 * * *")
@app.task(queue="default")
def close_shipped_jobs(timestamp: int) -> None:
    from . import fulfilment

    fulfilment.close_shipped()
