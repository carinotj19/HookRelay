"""Celery worker and Beat share the Flask configuration and DB session."""
from celery import Celery

from . import create_app
from .delivery import dispatch_due, perform_delivery

flask_app = create_app()
celery = Celery("hookrelay", broker=flask_app.config["REDIS_URL"])
celery.conf.update(
    task_ignore_result=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={"dispatch-due": {"task": "hookrelay.dispatch_due", "schedule": 5.0}},
)


@celery.task(name="hookrelay.deliver")
def deliver(delivery_id: str, token: str):
    with flask_app.app_context():
        return perform_delivery(delivery_id, token)


@celery.task(name="hookrelay.dispatch_due")
def dispatch():
    with flask_app.app_context():
        return dispatch_due(lambda delivery_id, token: deliver.apply_async(args=[delivery_id, token]))
