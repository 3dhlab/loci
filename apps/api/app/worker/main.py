import socket
from multiprocessing import get_context

from redis import Redis
from rq import Queue, Worker

from app.core.config import settings
from app.core.monitoring import configure_sentry
from app.services.publication_jobs import start_dispatcher, stop_dispatcher


def main() -> None:
    settings.validate_shared_secrets()
    configure_sentry("worker")
    redis_conn = Redis.from_url(settings.redis_url)
    queue_names = ["publication", "default", "transcode", "transcription", "index", "clip", "corpus_phrases"]
    queues = [Queue(name, connection=redis_conn) for name in queue_names]
    worker = Worker(queues, connection=redis_conn, name=f"{socket.gethostname()}.rq")
    dispatcher_context=get_context("spawn")
    dispatcher_stop=dispatcher_context.Event()
    dispatcher=start_dispatcher(dispatcher_stop,context=dispatcher_context)
    try:
        worker.work(with_scheduler=True)
    finally:
        stop_dispatcher(dispatcher,dispatcher_stop)


if __name__ == "__main__":
    main()
