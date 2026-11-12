"""Payload-free tracing and structured operational events."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
import time
import uuid

correlation_id = ContextVar('tollgate_correlation_id', default='')
logger = logging.getLogger('tollgate.operations')


def operational_event(name, **fields):
    allowed = {'workspace_id','resource_id','job_kind','state','error_code','duration_ms','method','route','status'}
    logger.info(json.dumps({'event':name,'correlation_id':correlation_id.get(),
                           **{k:v for k,v in fields.items() if k in allowed}}, sort_keys=True))


@contextmanager
def span(name, **attributes):
    from opentelemetry import trace
    start = time.perf_counter()
    tracer = trace.get_tracer('tollgate')
    with tracer.start_as_current_span(name, attributes=attributes) as current:
        try:
            yield current
        except Exception:
            operational_event(name, error_code='operation_failed')
            raise
        finally:
            operational_event(name, duration_ms=round((time.perf_counter()-start)*1000,2))


def traced(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate
