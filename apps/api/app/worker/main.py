import socket

from redis import Redis
from rq import Queue, Worker

from app.core.config import settings
from app.core.monitoring import configure_sentry


def main() -> None:
    settings.validate_shared_secrets()
    configure_sentry("worker")
    redis_conn = Redis.from_url(settings.redis_url)
    queue_names = ["default", "transcode", "transcription", "index", "clip", "corpus_phrases"]
    queues = [Queue(name, connection=redis_conn) for name in queue_names]
    worker = Worker(queues, connection=redis_conn, name=f"{socket.gethostname()}.rq")
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
