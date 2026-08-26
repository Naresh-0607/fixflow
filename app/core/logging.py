import logging


logger = logging.getLogger("fixflow")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

logger.propagate = False


def log_stage(job_id: str, message: str, *args: object) -> None:
    logger.info(f"[FixFlow] job={job_id} {message}", *args)
