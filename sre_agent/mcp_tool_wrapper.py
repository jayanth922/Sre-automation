#!/usr/bin/env python3
"""
MCP Tool Wrapper with Retry Logic and Structured Error Handling.

This module provides reliability hardening for MCP tool calls by:
1. Adding automatic retries with exponential backoff using tenacity
2. Returning structured ToolError objects instead of raising exceptions
3. Enabling graceful degradation when tools are unavailable
"""

import asyncio
import functools
import json
import logging
import os
from typing import Any, Callable, Optional
from datetime import datetime, timezone
import uuid

from pydantic import BaseModel
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    RetryError,
    before_sleep_log,
)

from .audit_context import get_audit_context
from .models import AgentAuditLog
# We need a session factory here. For now, we'll do a local import to avoid circular dep
# or assume the session is handled elsewhere. But for sync logging, we need a session.
from backend.database import SessionLocal

logger = logging.getLogger(__name__)


class ToolError(BaseModel):
    """Structured error returned when a tool fails after retries.
    
    This enables graceful degradation - the ReflectorNode can check for
    ToolError in findings and proceed without the failed tool's data.
    """
    tool_name: str
    error_message: str
    retry_count: int
    is_recoverable: bool = False
    suggestion: str = "Proceed with available data from other tools."
    
    def to_agent_response(self) -> str:
        """Format error for agent consumption."""
        return (
            f"Error: Tool {self.tool_name} failed after {self.retry_count} attempts. "
            f"Proceeding without this data. (Error: {self.error_message})"
        )


def is_tool_error(result: Any) -> bool:
    """Check if a result is a ToolError (either object or JSON string)."""
    if isinstance(result, ToolError):
        return True
    if isinstance(result, str):
        try:
            data = json.loads(result)
            return isinstance(data, dict) and "tool_name" in data and "error_message" in data
        except (json.JSONDecodeError, TypeError):
            pass
    return False


def parse_tool_error(result: Any) -> Optional[ToolError]:
    """Parse a ToolError from result if present."""
    if isinstance(result, ToolError):
        return result
    if isinstance(result, str):
        try:
            data = json.loads(result)
            if isinstance(data, dict) and "tool_name" in data:
                return ToolError(**data)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return None


def wrap_tool_with_retry(tool: Any, max_attempts: int = 3) -> Any:
    """
    Wrap a LangChain tool with tenacity retry logic.
    
    This wraps both sync (invoke) and async (ainvoke) methods with:
    - 3 retry attempts by default
    - Exponential backoff: 1s, 2s, 4s (max 10s)
    - Structured ToolError on final failure
    
    Args:
        tool: A LangChain BaseTool instance
        max_attempts: Maximum retry attempts before returning ToolError
        
    Returns:
        The same tool with wrapped invoke/ainvoke methods
    """
    tool_name = getattr(tool, 'name', 'unknown_tool')
    original_invoke = getattr(tool, 'invoke', None)
    original_ainvoke = getattr(tool, 'ainvoke', None)
    
    if original_invoke is None:
        logger.warning(f"Tool {tool_name} has no invoke method, skipping wrapper")
        return tool
    
    # Create retry decorator with logging
    retry_decorator = retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True  # We'll catch and handle in wrapper
    )
    
    # Wrap synchronous invoke
    @functools.wraps(original_invoke)
    def safe_invoke(*args, **kwargs) -> Any:
        @retry_decorator
        def invoke_with_retry():
            return original_invoke(*args, **kwargs)
        
        try:
            result = invoke_with_retry()
            return result
        except RetryError as e:
            last_exception = e.last_attempt.exception() if e.last_attempt else None
            error = ToolError(
                tool_name=tool_name,
                error_message=str(last_exception) if last_exception else "Unknown error after retries",
                retry_count=max_attempts,
                is_recoverable=False,
                suggestion=f"The {tool_name} tool is unavailable. Proceed with data from other tools."
            )
            logger.error(f"Tool {tool_name} failed after {max_attempts} retries: {error.error_message}")
            return error.to_agent_response()
        except Exception as e:
            error = ToolError(
                tool_name=tool_name,
                error_message=str(e),
                retry_count=1,
                is_recoverable=True,
                suggestion=f"Single failure in {tool_name}. Consider retrying manually."
            )
            logger.warning(f"Tool {tool_name} failed on first attempt: {e}")
            return error.to_agent_response()
    
    # Wrap asynchronous ainvoke if present
    if original_ainvoke is not None:
        @functools.wraps(original_ainvoke)
        async def safe_ainvoke(*args, **kwargs) -> Any:
            @retry_decorator
            async def ainvoke_with_retry():
                return await original_ainvoke(*args, **kwargs)
            
            try:
                result = await ainvoke_with_retry()
                return result
            except RetryError as e:
                last_exception = e.last_attempt.exception() if e.last_attempt else None
                error = ToolError(
                    tool_name=tool_name,
                    error_message=str(last_exception) if last_exception else "Unknown error after retries",
                    retry_count=max_attempts,
                    is_recoverable=False,
                    suggestion=f"The {tool_name} tool is unavailable. Proceed with data from other tools."
                )
                logger.error(f"Tool {tool_name} failed after {max_attempts} retries: {error.error_message}")
                return error.to_agent_response()
            except Exception as e:
                error = ToolError(
                    tool_name=tool_name,
                    error_message=str(e),
                    retry_count=1,
                    is_recoverable=True,
                    suggestion=f"Single failure in {tool_name}. Consider retrying manually."
                )
                logger.warning(f"Tool {tool_name} failed on first attempt: {e}")
                return error.to_agent_response()
        
        object.__setattr__(tool, "ainvoke", safe_ainvoke)
    
    object.__setattr__(tool, "invoke", safe_invoke)
    logger.debug(f"Wrapped tool {tool_name} with retry logic (max_attempts={max_attempts})")
    
    return tool



def log_audit_entry(
    tool_name: str, 
    status: str, 
    args: Any, 
    result: Any = None, 
    error: str = None,
    audit_id: uuid.UUID = None
) -> uuid.UUID:
    """
    Log an audit entry to the database and also push to the live terminal.
    """
    try:
        incident_id, agent_name = get_audit_context()
        
        # Serialize args/result safely
        args_str = str(args)
        result_str = str(result) if result else None
        if result_str and len(result_str) > 10000:
            result_str = result_str[:10000] + "... (truncated)"
            
        # Push a clean message to the Redis live terminal for the Dashboard
        from .redis_state_store import get_state_store
        state_store = get_state_store()
        if incident_id:
            timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            if status == "PENDING":
                msg = f"[{timestamp}] 🔧 EXECUTING: {tool_name} (args: {args_str[:100]}...)"
                state_store.append_log(str(incident_id), msg)
            elif status == "SUCCESS":
                msg = f"[{timestamp}] ✅ COMPLETED: {tool_name} returned success."
                state_store.append_log(str(incident_id), msg)
            elif status == "FAILURE":
                msg = f"[{timestamp}] ❌ FAILED: {tool_name} error: {error}"
                state_store.append_log(str(incident_id), msg)

        # Write to PostgreSQL for the Audit Log card
        with SessionLocal() as session:
            if audit_id:
                # Update existing log
                log_entry = session.get(AgentAuditLog, audit_id)
                if log_entry:
                    log_entry.status = status
                    log_entry.result = result_str
                    log_entry.error_message = error
                    session.commit()
                return audit_id
            else:
                # Create new log
                new_id = uuid.uuid4()
                log_entry = AgentAuditLog(
                    id=new_id,
                    timestamp=datetime.now(timezone.utc),
                    incident_id=incident_id or "general",
                    agent_name=agent_name or "SRE Agent",
                    tool_name=tool_name,
                    tool_args=args_str,
                    status=status
                )
                session.add(log_entry)
                session.commit()
                return new_id
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")
        return audit_id


def wrap_tool_with_audit(tool: Any) -> Any:
    """
    Wrap a tool to log execution to AgentAuditLog.
    """
    tool_name = getattr(tool, 'name', 'unknown_tool')
    original_invoke = getattr(tool, 'invoke', None)
    original_ainvoke = getattr(tool, 'ainvoke', None)
    
    if original_invoke:
        @functools.wraps(original_invoke)
        def audit_invoke(*args, **kwargs) -> Any:
            input_data = args[0] if args else kwargs
            audit_id = log_audit_entry(tool_name, "PENDING", input_data)
            try:
                result = original_invoke(*args, **kwargs)
                log_audit_entry(tool_name, "SUCCESS", input_data, result=result, audit_id=audit_id)
                return result
            except Exception as e:
                log_audit_entry(tool_name, "FAILURE", input_data, error=str(e), audit_id=audit_id)
                raise e
        # Use object.__setattr__ to bypass Pydantic immutability/validation
        object.__setattr__(tool, "invoke", audit_invoke)

    if original_ainvoke:
        @functools.wraps(original_ainvoke)
        async def audit_ainvoke(*args, **kwargs) -> Any:
            # Note: Writing to DB is sync, preventing blocking async loop might require run_in_executor
            # For now, we accept brief blocking for audit safety
            input_data = args[0] if args else kwargs
            audit_id = log_audit_entry(tool_name, "PENDING", input_data)
            try:
                result = await original_ainvoke(*args, **kwargs)
                log_audit_entry(tool_name, "SUCCESS", input_data, result=result, audit_id=audit_id)
                return result
            except Exception as e:
                log_audit_entry(tool_name, "FAILURE", input_data, error=str(e), audit_id=audit_id)
                raise e
        object.__setattr__(tool, "ainvoke", audit_ainvoke)
        
    return tool



# Circuit Breaker State (In-Memory for now, could be Redis)
_CIRCUIT_BREAKER_STATE = {
    "failures": {},  # tool_name -> count
    "last_failure": {}, # tool_name -> timestamp
    "is_open": {}, # tool_name -> bool
}

CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "5"))
CIRCUIT_BREAKER_RECOVERY_TIME = int(os.getenv("CIRCUIT_BREAKER_RECOVERY_SECONDS", "60"))

def check_circuit_breaker(tool_name: str) -> None:
    """Check if circuit breaker is open for tool."""
    if _CIRCUIT_BREAKER_STATE["is_open"].get(tool_name, False):
        last_fail = _CIRCUIT_BREAKER_STATE["last_failure"].get(tool_name)
        if last_fail:
            elapsed = (datetime.now(timezone.utc) - last_fail).total_seconds()
            if elapsed < CIRCUIT_BREAKER_RECOVERY_TIME:
                raise Exception(f"Circuit Breaker OPEN for {tool_name} (Cooling down for {int(CIRCUIT_BREAKER_RECOVERY_TIME - elapsed)}s)")
            else:
                # Half-open: Allow one triel
                logger.info(f"Circuit Breaker HALF-OPEN for {tool_name}")
                return
    return

def record_success(tool_name: str) -> None:
    """Reset failures on success."""
    if _CIRCUIT_BREAKER_STATE["failures"].get(tool_name, 0) > 0:
        logger.info(f"Circuit Breaker CLOSED for {tool_name} (Service recovered)")
        _CIRCUIT_BREAKER_STATE["failures"][tool_name] = 0
        _CIRCUIT_BREAKER_STATE["is_open"][tool_name] = False

def record_failure(tool_name: str) -> None:
    """Record failure and potentially open circuit."""
    current = _CIRCUIT_BREAKER_STATE["failures"].get(tool_name, 0) + 1
    _CIRCUIT_BREAKER_STATE["failures"][tool_name] = current
    _CIRCUIT_BREAKER_STATE["last_failure"][tool_name] = datetime.now(timezone.utc)
    
    if current >= CIRCUIT_BREAKER_THRESHOLD:
        if not _CIRCUIT_BREAKER_STATE["is_open"].get(tool_name, False):
            logger.warning(f"Circuit Breaker TRIPPED for {tool_name} after {current} failures")
        _CIRCUIT_BREAKER_STATE["is_open"][tool_name] = True


def wrap_tool_with_circuit_breaker(tool: Any) -> Any:
    """
    Wrap a tool with Circuit Breaker pattern.
    """
    tool_name = getattr(tool, 'name', 'unknown_tool')
    original_invoke = getattr(tool, 'invoke', None)
    original_ainvoke = getattr(tool, 'ainvoke', None)

    if original_invoke:
        @functools.wraps(original_invoke)
        def cb_invoke(*args, **kwargs) -> Any:
            check_circuit_breaker(tool_name)
            try:
                result = original_invoke(*args, **kwargs)
                record_success(tool_name)
                return result
            except Exception as e:
                record_failure(tool_name)
                raise e
        object.__setattr__(tool, "invoke", cb_invoke)

    if original_ainvoke:
        @functools.wraps(original_ainvoke)
        async def cb_ainvoke(*args, **kwargs) -> Any:
            check_circuit_breaker(tool_name)
            try:
                result = await original_ainvoke(*args, **kwargs)
                record_success(tool_name)
                return result
            except Exception as e:
                record_failure(tool_name)
                raise e
        object.__setattr__(tool, "ainvoke", cb_ainvoke)
        
    return tool


def wrap_all_tools_with_retry(tools: list, max_attempts: int = 3) -> list:
    """
    Wrap all tools in a list with:
    1. Retry Logic (Inner)
    2. Circuit Breaker (Middle)
    3. Audit Logic (Outer)
    
    Args:
        tools: List of LangChain BaseTool instances
        max_attempts: Maximum retry attempts per tool call
        
    Returns:
        List of wrapped tools
    """
    wrapped_tools = []
    for tool in tools:
        # 1. Add Retry Logic (Inner - retries temporary failures)
        retry_tool = wrap_tool_with_retry(tool, max_attempts)
        
        # 2. Add Circuit Breaker (Middle - stops calls if retries keep failing)
        cb_tool = wrap_tool_with_circuit_breaker(retry_tool)
        
        # 3. Add Audit Logic (Outer - Logs the final outcome)
        audit_tool = wrap_tool_with_audit(cb_tool)
        
        wrapped_tools.append(audit_tool)
    
    logger.info(f"Wrapped {len(wrapped_tools)} tools with Retry + CircuitBreaker + Audit")
    return wrapped_tools
