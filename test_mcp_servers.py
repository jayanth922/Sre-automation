#!/usr/bin/env python3

import asyncio
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

# Provide defaults for the host machine when running outside Docker
os.environ.setdefault("MCP_K8S_URI", "http://localhost:4000/sse")
os.environ.setdefault("MCP_METRICS_URI", "http://localhost:4001/sse")
os.environ.setdefault("MCP_LOGS_URI", "http://localhost:4002/sse")
os.environ.setdefault("MCP_GITHUB_URI", "http://localhost:4003/sse")
os.environ.setdefault("MCP_RUNBOOKS_URI", "http://localhost:4004/sse")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("REDIS_URL", "redis://localhost:6380")
os.environ.setdefault("POSTGRES_HOST", "localhost")

from sre_agent.multi_agent_langgraph import create_mcp_client

logging.basicConfig(level=logging.WARNING, format='%(message)s')
logger = logging.getLogger("test_mcp_servers")
logger.setLevel(logging.INFO)

# Suppress verbose LangGraph, HTTPX, internal agent trace logs, and Redis/DB warnings during smoke test
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("sre_agent").setLevel(logging.CRITICAL)

async def test_mcp_servers():
    """Smoke test to ensure MCP servers are accessible and returning tools."""
    logger.info("Starting MCP Server Smoke Test...")
    llm_provider = os.getenv("LLM_PROVIDER", "nvidia")
    
    client = create_mcp_client()
    try:
        tools = None
        last_error = None
        for attempt in range(1, 4):
            try:
                tools = await asyncio.wait_for(client.get_tools(), timeout=30.0)
                break
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Tool discovery attempt {attempt}/3 failed: {e}"
                )
                if attempt < 3:
                    await asyncio.sleep(3)

        if tools is None:
            raise RuntimeError(f"Unable to retrieve tools after 3 attempts: {last_error}")

        logger.info(f"✅ Successfully retrieved {len(tools)} tools from MCP servers.")
        
        # Categorize tools loosely by their names/prefixes
        tool_names = [t.name for t in tools]
        logger.info("Available Tools:")
        for name in tool_names:
            logger.info(f"  - {name}")
            
        if len(tools) == 0:
            logger.error("❌ No tools retrieved. Check MCP server URIs and configurations.")
            return False
            
        logger.info("Instantiating Specialist Agents for end-to-end tests...")
        logger.info(f"Using LLM provider for specialist tests: {llm_provider}")
        from sre_agent.agent_nodes import (
            create_kubernetes_agent,
            create_metrics_agent,
            create_logs_agent,
            create_github_agent,
            create_runbooks_agent
        )
        from sre_agent.mcp_tool_wrapper import wrap_all_tools_with_retry

        wrapped_tools = wrap_all_tools_with_retry(tools, max_attempts=1)
        agent_timeout_seconds = int(os.getenv("SMOKE_AGENT_TIMEOUT_SECONDS", "90"))
        agent_retry_attempts = int(os.getenv("SMOKE_AGENT_RETRY_ATTEMPTS", "2"))
        
        agents_to_test = [
            ("Kubernetes", create_kubernetes_agent, "Use your tools to list namespaces and report the result succinctly.", "kubernetes_agent"),
            ("Metrics", create_metrics_agent, "Use your tools to check Prometheus health and return the status.", "metrics_agent"),
            ("Logs", create_logs_agent, "Use your tools to fetch error logs for frontend in default namespace and return count.", "logs_agent"),
            ("GitHub", create_github_agent, "Use your tools to list the 3 most recent commits and return sha plus message.", "github_agent"),
            (
                "Runbooks",
                create_runbooks_agent,
                "Use your tools to search runbooks for database connection error and return top result.",
                "runbooks_agent",
            ),
        ]
        
        print("\n" + "="*50)
        print("   SPECIALIST AGENT SMOKE TESTS")
        print("="*50)
        specialist_failures = []

        def _looks_transient_failure(text: str) -> bool:
            lowered = text.lower()
            return (
                "internal server error" in lowered
                or "status code: -1" in lowered
                or "connection attempts failed" in lowered
                or "service unavailable" in lowered
            )
        
        for name, factory, query, result_key in agents_to_test:
            print(f"\n[{name} Agent]")
            print(f"Asked: '{query}'")
            try:
                attempt = 0
                last_safe_response = ""
                while attempt < agent_retry_attempts:
                    attempt += 1
                    agent = factory(
                        wrapped_tools,
                        llm_provider=llm_provider,
                        max_tokens=512,
                        temperature=0.0,
                    )
                    state = {
                        "messages": [],
                        "current_query": query,
                        "agents_invoked": [],
                        "agent_results": {},
                        "metadata": {}
                    }

                    try:
                        result = await asyncio.wait_for(agent(state), timeout=agent_timeout_seconds)
                    except asyncio.TimeoutError:
                        last_safe_response = (
                            f"Agent execution timed out after {agent_timeout_seconds} seconds."
                        )
                        if attempt < agent_retry_attempts:
                            print(
                                f"Transient timeout detected. Retrying {name} agent "
                                f"({attempt}/{agent_retry_attempts})..."
                            )
                            await asyncio.sleep(2)
                            continue
                        specialist_failures.append(
                            f"{name} agent timed out after {agent_timeout_seconds} seconds"
                        )
                        print(f"Response:\n{last_safe_response}")
                        break

                    agent_response = result.get("agent_results", {}).get(result_key, "No response returned.")
                    safe_response = agent_response.strip().encode('ascii', 'ignore').decode('ascii')
                    last_safe_response = safe_response

                    lowered = safe_response.lower()
                    hard_failure = (
                        "agent execution failed" in lowered
                        or "timed out" in lowered
                        or "error:" in lowered
                        or "all connection attempts failed" in lowered
                    )

                    if hard_failure and _looks_transient_failure(safe_response) and attempt < agent_retry_attempts:
                        print(
                            f"Transient provider/tool error detected. Retrying {name} agent "
                            f"({attempt}/{agent_retry_attempts})..."
                        )
                        await asyncio.sleep(2)
                        continue

                    print(f"Response:\n{safe_response}")

                    if hard_failure:
                        specialist_failures.append(
                            f"{name} agent reported failure: {safe_response[:200]}"
                        )
                    break
            except Exception as e:
                print(f"Error during {name} agent test: {e}")
                specialist_failures.append(f"{name} agent exception: {e}")

        print("\n" + "="*50)
        if specialist_failures:
            logger.error("❌ Specialist agent smoke tests reported failures:")
            for failure in specialist_failures:
                logger.error(f"  - {failure}")
            return False

        logger.info("✅ Smoke test passed.")
        return True
    except Exception as e:
        logger.error(f"❌ Smoke test failed: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_mcp_servers())
    if not success:
        exit(1)
