#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient

from .agent_state import AgentState
from .constants import SREConstants
from .graph_builder import build_multi_agent_graph
from .logging_config import configure_logging, should_show_debug_traces
from .execution_context import (
    ExecutionContext,
    is_production_runtime,
    require_execution_context,
    require_operator_mcp_endpoint,
)

# Configure logging if not already configured (e.g., when imported by agent_runtime)
if not logging.getLogger().handlers:
    from .config import parse_bool

    debug_from_env = parse_bool(os.getenv("DEBUG"), default=False, name="DEBUG")
    configure_logging(debug_from_env)

logger = logging.getLogger(__name__)

# Load environment variables from .env file in sre_agent directory
load_dotenv(Path(__file__).parent / ".env")


def _get_user_from_env() -> str:
    """Get user_id from environment variable.

    Returns:
        user_id from USER_ID environment variable or default
    """
    user_id = os.getenv("USER_ID")
    if user_id:
        logger.info(f"Using user_id from environment: {user_id}")
        return user_id
    else:
        # Fallback to default user_id
        default_user_id = SREConstants.agents.default_user_id
        logger.warning(
            f"USER_ID not set in environment, using default: {default_user_id}"
        )
        return default_user_id






@tool
def get_current_time() -> str:
    """Get current date and time in ISO format.

    This tool provides the current timestamp which is essential for debugging
    time-sensitive issues and correlating events across different systems.

    Returns:
        str: Current datetime in ISO format (YYYY-MM-DDTHH:MM:SS)
    """
    return datetime.now().isoformat()


def build_mcp_server_config(
    context: ExecutionContext,
    *,
    service_token: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """Build a tenant-bound MCP config with mandatory bearer auth."""
    server_config: dict[str, dict[str, Any]] = {}
    for name, uri in context.mcp_endpoints.items():
        if uri.startswith("stdio://"):
            if is_production_runtime():
                raise RuntimeError("STDIO MCP endpoints are disabled in production")
            parts = uri.removeprefix("stdio://").split(":", 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid stdio MCP endpoint for {name}")
            server_config[name] = {
                "command": parts[0],
                "args": [parts[1]],
                "transport": "stdio",
            }
            continue
        uri = require_operator_mcp_endpoint(name, uri)
        server_config[name] = {
            "url": uri,
            "transport": "sse",
            "headers": dict(context.transport_headers(service_token)),
        }
    if not server_config:
        raise ValueError(
            f"Cluster {context.cluster_id} has no tenant-bound MCP endpoints"
        )
    return server_config


def create_mcp_client(
    context: Optional[ExecutionContext] = None,
    *,
    service_token: Optional[str] = None,
) -> MultiServerMCPClient:
    """
    Create and return MultiServerMCPClient with appropriate transport for each domain.
    
    Supports:
    - HTTP/SSE transport: For native FastMCP servers running in Docker/K8s
    - STDIO transport: For local development (if URI starts with "stdio://")
    """
    execution_context = require_execution_context(context)
    return MultiServerMCPClient(
        build_mcp_server_config(execution_context, service_token=service_token)
    )


async def close_mcp_client(client: Any) -> None:
    """Best-effort teardown across MCP adapter versions."""
    if client is None:
        return
    for method_name in ("aclose", "close"):
        method = getattr(client, method_name, None)
        if method is None:
            continue
        result = method()
        if asyncio.iscoroutine(result):
            await result
        return


async def create_multi_agent_system(
    provider: str = "groq",
    checkpointer=None,
    export_graph: bool = False,
    graph_output_path: str = "./graph_architecture.md",
    execution_context: Optional[ExecutionContext] = None,
    return_client: bool = False,
    **llm_kwargs,
):
    """Create multi-agent system with MCP tools."""
    context = require_execution_context(execution_context)
    provider = context.llm_provider or provider
    context_llm_kwargs = context.llm_kwargs()
    context_llm_kwargs.update(llm_kwargs)
    llm_kwargs = context_llm_kwargs
    logger.info(f"Creating multi-agent system with provider: {provider}")

    if provider not in ["groq", "anthropic", "openai_compatible"]:
        raise ValueError(f"Unsupported provider: {provider}. Supported: 'anthropic', 'groq', 'openai_compatible'.")

    # Create MCP client and get tools with retry logic
    mcp_tools = []
    max_retries = 3
    retry_count = 0

    client = None
    while retry_count < max_retries:
        try:
            client = create_mcp_client(context)
            
            # Use get_tools() but handle potential ExceptionGroups from failing servers
            try:
                # Add timeout for MCP tool loading to prevent hanging
                all_mcp_tools = await asyncio.wait_for(
                    client.get_tools(),
                    timeout=SREConstants.timeouts.mcp_tools_timeout_seconds,
                )
            except (asyncio.TimeoutError, Exception) as e:
                # If we have an ExceptionGroup or multiple errors, log and potentially partial results
                logger.warning(f"MCP tool loading encountered issues: {e}")
                # Some versions of the client might still return partial tools or we might need to retry
                if retry_count < max_retries - 1:
                    await close_mcp_client(client)
                    client = None
                    raise e # Trigger retry
                all_mcp_tools = [] # Fallback to empty on last retry
                # may need the client to remain connected to be invoked.
                # However, for infra tools that are dispatched to the edge, 
                # this client is only used for discovery.
                pass

            # Wrap MCP tools with retry logic for resilience
            from .mcp_tool_wrapper import wrap_all_tools_with_retry
            mcp_tools = wrap_all_tools_with_retry(all_mcp_tools, max_attempts=3)

            logger.info(f"Retrieved {len(mcp_tools)} tools from MCP")

            # Print tool information (only in debug mode)
            logger.info(f"MCP tools loaded: {len(mcp_tools)}")
            if should_show_debug_traces():
                print(f"\nMCP tools loaded: {len(mcp_tools)}")
                for tool in mcp_tools:
                    tool_name = getattr(tool, "name", "unknown")
                    tool_desc = getattr(tool, "description", "No description")
                    print(f"  - {tool_name}: {tool_desc[:80]}...")
                    logger.info(f"  - {tool_name}: {tool_desc[:80]}...")

            # Success - break out of retry loop
            break

        except asyncio.TimeoutError:
            logger.warning("MCP tool loading timed out after 30 seconds")
            await close_mcp_client(client)
            client = None
            mcp_tools = []
            break  # Don't retry on timeout

        except Exception as e:
            await close_mcp_client(client)
            client = None
            retry_count += 1
            error_msg = str(e)

            # Check if it's a rate limiting error (429)
            if "429" in error_msg or "Too Many Requests" in error_msg:
                if retry_count < max_retries:
                    # Exponential backoff with jitter
                    base_delay = 2**retry_count  # 2, 4, 8 seconds
                    jitter = random.uniform(0, 1)  # Add 0-1 second random jitter
                    wait_time = base_delay + jitter

                    logger.warning(
                        f"Rate limited by MCP server (attempt {retry_count}/{max_retries}). "
                        f"Waiting {wait_time:.1f} seconds before retry..."
                    )
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(
                        f"Failed to load MCP tools after {max_retries} retries: {e}"
                    )
                    mcp_tools = []
            else:
                # For other errors, don't retry
                logger.warning(f"Failed to load MCP tools: {e}")
                mcp_tools = []
                break

    # Combine local tools with MCP tools
    local_tools = [get_current_time]
    all_tools = local_tools + mcp_tools

    # Debug: Show all tools being passed to agents
    logger.info(f"Total tools being passed to agents: {len(all_tools)}")
    logger.info(f"  - Local tools: {len(local_tools)}")
    logger.info(f"  - MCP tools: {len(mcp_tools)}")

    logger.info("All tool names:")
    for tool in all_tools:
        tool_name = getattr(tool, "name", "unknown")
        tool_description = getattr(tool, "description", "No description")
        # Extract just the first line of description for cleaner logging
        description_first_line = (
            tool_description.split("\n")[0].strip()
            if tool_description
            else "No description"
        )
        logger.info(f"  - {tool_name}: {description_first_line}")

    logger.info(f"Additional local tools: {len(local_tools)}")
    if should_show_debug_traces():
        print(f"\nAdditional local tools: {len(local_tools)}")
        for tool in local_tools:
            # Extract just the first line of description
            description = (
                tool.description.split("\n")[0].strip()
                if tool.description
                else "No description"
            )
            print(f"  - {tool.name}: {description}")
            logger.info(f"  - {tool.name}: {description}")

    # Default the checkpointer from env config when one wasn't supplied. Returns
    # None unless CHECKPOINTER_ENABLED=true, so the default path is unchanged.
    if checkpointer is None:
        from .checkpointer import get_checkpointer
        checkpointer = await get_checkpointer()

    # Build the multi-agent graph
    graph = build_multi_agent_graph(
        tools=all_tools,
        llm_provider=provider,
        checkpointer=checkpointer, # Pass checkpointer for persistence
        execution_context=context,
        export_graph=export_graph,
        graph_output_path=graph_output_path,
        **llm_kwargs,
    )

    if return_client:
        return graph, all_tools, client
    return graph, all_tools
