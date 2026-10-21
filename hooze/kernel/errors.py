def describe_exc(e: BaseException, limit: int = 200) -> str:
    """Readable text for logs/alerts. Many network errors (timeouts, resets) have an EMPTY message: in the first real logs a failed
    maintenance step was recorded as `"error": ""`, which says nothing. Fall back to the exception's type."""
    return (str(e) or type(e).__name__)[:limit]
