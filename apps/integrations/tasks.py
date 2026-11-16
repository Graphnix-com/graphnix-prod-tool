from procrastinate import RetryStrategy
from procrastinate.contrib.django import app

from . import square

# 12 tries, 2 s apart at first and doubling: about 2 hours 15 minutes in all. After the last
# one the failure is logged, which sends the crash email; the nightly check retries it too.
RETRY = RetryStrategy(max_attempts=12, exponential_wait=2)


@app.task(queue="default", retry=RETRY)
def process_webhook_event(event_pk: int) -> None:
    square.process_event(event_pk)


# 10:00 UTC = 03:00 in Alberta in winter (04:00 in summer): quiet either way.
@app.periodic(cron="0 10 * * *")
@app.task(queue="default")
def square_nightly_check(timestamp: int) -> None:
    from . import nightly

    nightly.run()


# Square's estimate emails (its API has no estimates): every 5 minutes, when set up.
@app.periodic(cron="*/5 * * * *")
@app.task(queue="default")
def check_estimate_mail(timestamp: int) -> None:
    from . import estimates

    if estimates.enabled():
        estimates.check_mail()
