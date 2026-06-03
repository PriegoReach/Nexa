from celery import Celery

from app.core.config import settings

celery_app = Celery("nexaagent", broker=settings.celery_broker_url)
celery_app.conf.update(task_track_started=True, task_serializer="json")

# Ensure tasks are registered when the worker boots.
celery_app.autodiscover_tasks(["app.workers"])
