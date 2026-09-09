"""Deployment entrypoint. Configuration and existing accounts must be provisioned."""
import os
from .deployment import load_configuration


def create_app():
    from .api import create_app as build_app
    if os.environ.get('BIGBASE_LOCAL_HTTP') == '1':
        raise RuntimeError('INSECURE_COOKIE_CONFIGURATION_FORBIDDEN')
    path = os.environ.get('BIGBASE_DEPLOYMENT_FILE')
    if not path:
        raise RuntimeError('EXPLICIT_DEPLOYMENT_FILE_REQUIRED')
    reads = None
    try:
        runtime, reads = load_configuration(path)
        return build_app(canonical_reads=reads, deployment=runtime)
    except Exception:
        if reads is not None:reads.close()
        raise RuntimeError('DEPLOYMENT_CONFIGURATION_OR_DATABASE_UNAVAILABLE') from None
