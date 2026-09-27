import socket

from redis import Redis
from rq import Worker

from app.core.config import settings


def main() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    redis_conn.ping()

    expected_worker_name = f"{socket.gethostname()}.rq"
    workers = Worker.all(connection=redis_conn)
    if any(worker.name == expected_worker_name for worker in workers):
        print(expected_worker_name)
        return

    raise SystemExit(f"Worker is not registered in Redis: {expected_worker_name}")


if __name__ == "__main__":
    main()