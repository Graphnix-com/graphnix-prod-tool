from procrastinate import RetryStrategy
from procrastinate.contrib.django import app

from . import square

# About 2 hours of tries with back-off. After the last one the failure is logged, which
# sends the crash email; the event stays stored and the nightly check catches it up.
RETRY = RetryStrategy(max_attempts=10, exponential_wait=5)


@app.task(queue="default", retry=RETRY)
def process_webhook_event(event_pk: int) -> None:
    square.process_event(event_pk)
