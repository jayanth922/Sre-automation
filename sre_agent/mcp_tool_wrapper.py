#!/usr/bin/env python3
"""
MCP Tool Wrapper with Retry Logic and Structured Error Handling.

This module provides reliability hardening for MCP tool calls by:
1. Adding automatic retries with exponential backoff using tenacity
2. Raising `ToolExecutionError` — a structured `ToolError` inside an exception
   — when a tool is exhausted, so that every layer above (circuit breaker,
   audit log, langgraph's ToolNode, the supervisor's caveats) can tell a
   failure from an answer. It used to *return* the error text, which read as
   success everywhere and is the defect `ToolExecutionError` documents.
3. Refusing tools that only approved remediation may call
   (`investigation_write_guard`)
4. Enabling graceful degradation when tools are unavailable
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

from .audit_context import get_audit_context, note_audit_write_failure
from .investigation_write_guard import (
    ToolNotAuthorizedError,
    wrap_tool_with_write_guard,
)
from backend.models import AgentAuditLog
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


class ToolExecutionError(Exception):
    """A tool failure that is still a failure by the time anyone reads it.

    The retry wrapper used to `return error.to_agent_response()` — a plain
    string. Every layer above it is built to notice an exception, so returning
    made a total outage indistinguishable from a successful call. Probed on
    2026-09-15 against a tool that raises `ConnectionError` on every attempt:

        returned type : str
        is_tool_error : False        # the module cannot parse its own output
        cb failures   : {}           # circuit breaker recorded nothing
        audit statuses: ['PENDING', 'SUCCESS']

    Four consequences, all of them silent:

    * `record_failure` is unreachable through `wrap_all_tools_with_retry`'s
      composition, so the circuit breaker can never open and a dead MCP server
      is re-dialled with full backoff forever.
    * `AgentAuditLog` — the provenance record — stores the outage as SUCCESS,
      with the error text filed as the result.
    * langgraph's `ToolNode` only sets `ToolMessage.status == "error"` when the
      bound tool *raises*. `agent_nodes.py` calls that "the ONLY reliable
      signal for 'the tool itself failed'" and keys `tool_failures` off it, so
      `agent_tool_failures` was structurally always empty and the six places in
      `supervisor.py` that caveat a conclusion with it never fired. The system
      could not say "I concluded this with the metrics tool down."
    * `is_tool_error`/`parse_tool_error` parse JSON; `to_agent_response()`
      emits prose. The graceful-degradation contract in `ToolError`'s own
      docstring was broken at both ends.

    Raising is safe for the one production caller, but not for free: these
    tools are bound into `create_react_agent`, and langgraph's default handler
    absorbs only `ToolInvocationError` and re-raises the rest, so an unhandled
    raise here would kill the investigation instead of degrading it. That is
    why `agent_nodes.py` passes an explicit
    `ToolNode(tools, handle_tool_errors=handle_tool_execution_error)`: this
    type — and only this type — becomes a `ToolMessage(status="error")`. The
    agent still gets a readable explanation and still continues; it just now
    gets a *typed* one, and so does everybody else on the way up.
    """

    def __init__(self, error: "ToolError"):
        super().__init__(error.to_agent_response())
        self.tool_error = error


@functools.lru_cache(maxsize=1)
def policy_refusals() -> tuple:
    """The exception types that mean "we decided not to run this".

    Both subclass `PermissionError`, but matching on `PermissionError` itself
    would sweep in a real 403 from a cluster we genuinely lack RBAC for —
    an environment failure the on-call needs to see as a failure, not as our
    own policy. So the two are named.
    """
    from .namespace_scope import NamespaceScopeError

    return (ToolNotAuthorizedError, NamespaceScopeError)


def handle_tool_execution_error(exc: Exception) -> str:
    """`handle_tool_errors` hook for the `ToolNode` behind each specialist.

    langgraph's own default only absorbs `ToolInvocationError` (bad arguments)
    and re-raises everything else, which would turn a dead MCP server into a
    dead investigation. Verified against the installed version by running a
    raising tool through a compiled graph: the `ConnectionError` propagated out
    of the node and killed the run.

    So the node needs to be told about `ToolExecutionError` explicitly. Doing
    it here rather than with `handle_tool_errors=True` keeps the blanket catch
    off: a genuinely unexpected exception is still a crash we want to see, not
    a sentence dropped into the model's context.

    Returning the message makes langgraph emit `ToolMessage(status="error")`,
    which is the signal `agent_nodes.py` records in `tool_failures`.

    A policy refusal is absorbed for a second reason, which a probe against
    a compiled graph on 2026-09-15 made concrete: an exception that leaves
    this node kills the node, and any *sibling* tool call langgraph started
    in the same parallel batch dies unfinished with it. So a refusal that
    escaped here would not merely go unread by the model — it would take the
    read-only calls beside it down too, which is the opposite of what the
    refusal text asks the agent to do. Both refusal types carry a message
    written to be read by the model, so returning it is the whole point.
    """
    if isinstance(exc, ToolExecutionError):
        return str(exc)
    if isinstance(exc, policy_refusals()):
        return str(exc)
    from langgraph.prebuilt.tool_node import ToolInvocationError

    if isinstance(exc, ToolInvocationError):
        return getattr(exc, "message", str(exc))
    raise exc


def _exhausted(tool_name: str, exc: BaseException, attempts: int) -> ToolError:
    return ToolError(
        tool_name=tool_name,
        error_message=str(exc) if exc else "Unknown error after retries",
        retry_count=attempts,
        is_recoverable=False,
        suggestion=f"The {tool_name} tool is unavailable. Proceed with data from other tools.",
    )


def _attempts_made(exc: "RetryError", default: int) -> int:
    """How many times the tool was actually called.

    The old code reported `retry_count=1` and logged "failed on first attempt"
    after tenacity had already retried, because `reraise=True` re-raises the
    *original* exception and the `except RetryError` branch was dead. The audit
    trail recorded a wrong attempt count for every exhausted tool.
    """
    last = getattr(exc, "last_attempt", None)
    return getattr(last, "attempt_number", default) or default


def is_tool_error(result: Any) -> bool:
    """Check if a result is a ToolError (object, raised error, or JSON string)."""
    if isinstance(result, ToolExecutionError):
        return True
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
    if isinstance(result, ToolExecutionError):
        return result.tool_error
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
    # `reraise=False` so tenacity raises RetryError once the attempts are spent.
    # With reraise=True it re-raised the original exception instead, which made
    # the `except RetryError` branch below dead code: every exhausted tool fell
    # into the generic handler and was recorded as a single recoverable failure.
    retry_decorator = retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=False,
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
            attempts = _attempts_made(e, max_attempts)
            error = _exhausted(tool_name, last_exception, attempts)
            logger.error(f"Tool {tool_name} failed after {attempts} attempts: {error.error_message}")
            raise ToolExecutionError(error) from last_exception or e
        except Exception as e:
            error = ToolError(
                tool_name=tool_name,
                error_message=str(e),
                retry_count=1,
                is_recoverable=True,
                suggestion=f"Single failure in {tool_name}. Consider retrying manually."
            )
            logger.warning(f"Tool {tool_name} failed without retrying: {e}")
            raise ToolExecutionError(error) from e
    
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
                attempts = _attempts_made(e, max_attempts)
                error = _exhausted(tool_name, last_exception, attempts)
                logger.error(f"Tool {tool_name} failed after {attempts} attempts: {error.error_message}")
                raise ToolExecutionError(error) from last_exception or e
            except Exception as e:
                error = ToolError(
                    tool_name=tool_name,
                    error_message=str(e),
                    retry_count=1,
                    is_recoverable=True,
                    suggestion=f"Single failure in {tool_name}. Consider retrying manually."
                )
                logger.warning(f"Tool {tool_name} failed without retrying: {e}")
                raise ToolExecutionError(error) from e
        
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
        (
            incident_id,
            agent_name,
            organization_id,
            cluster_id,
            run_id,
        ) = get_audit_context()
        
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
            elif status == "REFUSED":
                # Not a failure — nothing was called. An operator needs to see
                # this one *more* than a failure, not less.
                msg = f"[{timestamp}] 🚫 REFUSED: {tool_name} — {error}"
                state_store.append_log(str(incident_id), msg)
            elif status == "CANCELLED":
                # The reader's alternative is the 🔧 EXECUTING line above,
                # still standing, hours after the call was abandoned.
                msg = f"[{timestamp}] ⏹️ CANCELLED: {tool_name} — {error}"
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
                    organization_id=(
                        uuid.UUID(str(organization_id)) if organization_id else None
                    ),
                    cluster_id=(uuid.UUID(str(cluster_id)) if cluster_id else None),
                    incident_id=incident_id,
                    run_id=run_id,
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
        note_audit_write_failure(str(e))
        return audit_id


def _audit_status_for(exc: BaseException) -> str:
    """"The tool broke", "we did not let it run" and "we abandoned it" are
    three different events.

    All three end in an exception here, and collapsing them into FAILURE
    would file an attempted unapproved write alongside a flaky Prometheus —
    and, worse, would still leave the abandoned call with no terminal row at
    all, because `CancelledError` is a `BaseException` and never reached the
    old `except Exception`. Live on 2026-09-15: 18 `agent_audit_logs` rows
    sat at PENDING forever, always in same-millisecond bursts, because one
    tool in a parallel batch raised and langgraph cancelled its siblings.
    """
    if isinstance(exc, policy_refusals()):
        return "REFUSED"
    if isinstance(exc, asyncio.CancelledError):
        return "CANCELLED"
    return "FAILURE"


def _audit_error_text(exc: BaseException) -> str:
    """`str(CancelledError())` is the empty string, and an audit row whose
    error column is blank says nothing about why the call ended."""
    text = str(exc)
    if text:
        return text
    if isinstance(exc, asyncio.CancelledError):
        return (
            "Cancelled before it returned — another tool in the same parallel "
            "batch raised, so langgraph tore down its siblings."
        )
    return type(exc).__name__


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
            except BaseException as e:
                log_audit_entry(
                    tool_name,
                    _audit_status_for(e),
                    input_data,
                    error=_audit_error_text(e),
                    audit_id=audit_id,
                )
                raise
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
            except BaseException as e:
                # BaseException, not Exception: a sibling tool call cancelled
                # by langgraph raises CancelledError, which the narrower
                # clause let past without ever closing the PENDING row.
                log_audit_entry(
                    tool_name,
                    _audit_status_for(e),
                    input_data,
                    error=_audit_error_text(e),
                    audit_id=audit_id,
                )
                raise
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


def wrap_tool_with_namespace_scope(tool: Any, context: Any) -> Any:
    """Enforce the execution context on namespaced MCP tool inputs."""
    from .namespace_scope import enforce_tool_arguments

    tool_name = getattr(tool, "name", "unknown_tool")
    original_invoke = getattr(tool, "invoke", None)
    original_ainvoke = getattr(tool, "ainvoke", None)

    def scoped_call(call_args, call_kwargs):
        args = list(call_args)
        kwargs = dict(call_kwargs)
        if args:
            args[0] = enforce_tool_arguments(tool_name, args[0], context)
        elif "input" in kwargs:
            kwargs["input"] = enforce_tool_arguments(
                tool_name, kwargs["input"], context
            )
        elif "tool_input" in kwargs:
            kwargs["tool_input"] = enforce_tool_arguments(
                tool_name, kwargs["tool_input"], context
            )
        else:
            args.append(enforce_tool_arguments(tool_name, {}, context))
        return tuple(args), kwargs

    if original_invoke is not None:
        @functools.wraps(original_invoke)
        def scoped_invoke(*args, **kwargs):
            scoped_args, scoped_kwargs = scoped_call(args, kwargs)
            return original_invoke(*scoped_args, **scoped_kwargs)

        object.__setattr__(tool, "invoke", scoped_invoke)

    if original_ainvoke is not None:
        @functools.wraps(original_ainvoke)
        async def scoped_ainvoke(*args, **kwargs):
            scoped_args, scoped_kwargs = scoped_call(args, kwargs)
            return await original_ainvoke(*scoped_args, **scoped_kwargs)

        object.__setattr__(tool, "ainvoke", scoped_ainvoke)

    return tool


def wrap_all_tools_with_retry(
    tools: list,
    max_attempts: int = 3,
    *,
    execution_context: Any = None,
) -> list:
    """
    Wrap all tools in a list with, innermost first:
    1. Retry Logic
    2. Circuit Breaker
    3. Namespace Scope (when an execution context is supplied)
    4. Write Guard — refuses tools only approved remediation may call
    5. Audit Logic (Outer)

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
        
        scoped_tool = cb_tool
        if execution_context is not None:
            scoped_tool = wrap_tool_with_namespace_scope(cb_tool, execution_context)

        # 3. Refuse remediation-only tools outright. These tools are reachable
        # only through the approved executor path; a specialist holding one is
        # a configuration mistake, and calling it would bypass the human.
        guarded_tool = wrap_tool_with_write_guard(scoped_tool)

        # Audit remains outermost so rejected scope attempts and refused
        # writes are both recorded.
        audit_tool = wrap_tool_with_audit(guarded_tool)
        
        wrapped_tools.append(audit_tool)
    
    logger.info(
        "Wrapped %s tools with Retry + CircuitBreaker + Namespace + WriteGuard + Audit",
        len(wrapped_tools),
    )
    return wrapped_tools
