from vector_lake import tool_ingest
def sync_vector_lake(*, _progress: dict | None = None):
    """Compatibility result; internal progress lets a single worker drain full batches."""
    if _progress is None:
        res = tool_ingest.prepare_ingest_batch(batch_size=50)
    else:
        res = tool_ingest.prepare_ingest_batch(batch_size=50, _progress=_progress)
    return f"Legacy Sync prepared ingest work. Index updates are committed through the durable outbox.\n\n{res}"

