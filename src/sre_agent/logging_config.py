#!/usr/bin/env python3

import logging
import json
import os
import sys
from datetime import datetime
from typing import Optional, Any

class JSONFormatter(logging.Formatter):
    """
    Formatter that outputs JSON strings for all logs.
    """
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "process": record.process,
            "thread": record.threadName,
        }

        # Add exception info if present
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields from record (if any)
        if hasattr(record, "extra"):
             log_obj.update(record.extra)

        return json.dumps(log_obj)

def _configure_http_loggers(debug_enabled: bool = False) -> None:
    """Configure HTTP client loggers based on debug setting."""
    http_loggers = [
        "httpx",
        "httpcore",
        "streamable_http",
        "mcp.client.streamable_http",
    ]

    for logger_name in http_loggers:
        http_logger = logging.getLogger(logger_name)
        if debug_enabled:
            http_logger.setLevel(logging.DEBUG)
        else:
            http_logger.setLevel(logging.WARNING)


def configure_logging(debug: Optional[bool] = None) -> bool:
    """Configure logging with JSON formatter based on debug setting.

    Args:
        debug: Enable debug logging. If None, checks DEBUG environment variable.

    Returns:
        bool: Whether debug logging is enabled
    """
    # Determine debug setting
    if debug is None:
        from sre_agent.config import parse_bool

        debug = parse_bool(os.getenv("DEBUG"), default=False, name="DEBUG")

    # Set log level based on debug setting
    log_level = logging.DEBUG if debug else logging.INFO

    # Create handler with JSON Formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Remove existing handlers to avoid duplicates/mixed formats
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    
    root_logger.addHandler(handler)

    # Configure HTTP loggers
    _configure_http_loggers(debug)

    # Configure MCP logger
    mcp_logger = logging.getLogger("mcp")
    if debug:
        mcp_logger.setLevel(logging.DEBUG)
    else:
        mcp_logger.setLevel(logging.WARNING)
    
    # Force sqlalchemy to be less noisy unless debug
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    return debug


def should_show_debug_traces() -> bool:
    """Check if debug traces should be shown."""
    from sre_agent.config import parse_bool

    return parse_bool(os.getenv("DEBUG"), default=False, name="DEBUG")
