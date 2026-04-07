#!/usr/bin/env python3

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, StateGraph

from .agent_nodes import (
    create_github_agent,
    create_kubernetes_agent,
    create_logs_agent,
    create_metrics_agent,
    create_runbooks_agent,
)
from .agent_state import (
    AgentState,
    InvestigationFindings,
    ReflectorAnalysis,
    RemediationAction,
    RemediationPlan,
)
from .constants import SREConstants
from .llm_utils import create_llm_with_error_handling
from .policy_engine import (
    calculate_risk_score,
    evaluate_action,
    get_environment_from_context,
)
from .supervisor import SupervisorAgent

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


def _route_supervisor(state: AgentState) -> str:
    """Route from supervisor to the appropriate node based on OODA phase."""
    ooda_phase = state.get("ooda_phase", "OBSERVE")
    next_node = state.get("next", "investigation_swarm")

    logger.info(f"Supervisor routing: OODA phase={ooda_phase}, next={next_node}")

    # Map next to actual node names
    node_map = {
        "investigation_swarm": "investigation_swarm",
        "reflector": "reflector",
        "planner": "planner",
        "aggregate": "aggregate",
        "FINISH": "aggregate",
    }

    return node_map.get(next_node, "investigation_swarm")


async def _prepare_initial_state(state: AgentState) -> Dict[str, Any]:
    """Prepare the initial state with the user's query or alert context."""
    messages = state.get("messages", [])

    # Extract the current query from the last human message
    current_query = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            current_query = msg.content
            break

    # Determine if this is an alert-driven investigation
    alert_context = state.get("alert_context")
    is_alert_driven = alert_context is not None

    # Set initial OODA phase
    ooda_phase = "OBSERVE" if is_alert_driven else "OBSERVE"

    # Get llm_provider from existing metadata or use default
    existing_metadata = state.get("metadata", {})
    llm_provider = existing_metadata.get("llm_provider", "ollama")

    return {
        "current_query": current_query,
        "ooda_phase": ooda_phase,
        "agent_results": {},
        "agents_invoked": [],
        "requires_collaboration": True,  # Always true for investigation swarm
        "metadata": {
            **existing_metadata,
            "llm_provider": llm_provider,
        },
        "next": "investigation_swarm",
        "thought_traces": {},
        "investigation_count": 0,
    }


async def _investigation_swarm(state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    InvestigationSwarm: Parallel execution of InfraAgent and CodeAgent.
    
    This implements the OBSERVE phase of the OODA loop by gathering
    evidence from multiple sources simultaneously.
    """
    logger.info("🔍 InvestigationSwarm: Starting parallel investigation")

    # Prepare investigation query
    alert_context = state.get("alert_context")
    current_query = state.get("current_query", "")

    if alert_context:
        investigation_query = f"""
        Alert: {alert_context.alert_name}
        Severity: {alert_context.severity}
        Labels: {alert_context.labels}
        Description: {alert_context.annotations.get('description', '')}
        
        Investigate this alert and gather evidence from infrastructure and code changes.
        Reference Golden Signals: Latency, Traffic, Errors, Saturation.
        """
    else:
        investigation_query = current_query or "Investigate system health and identify issues."

    # Get agent instances from metadata (passed from graph builder)
    metadata = state.get("metadata", {})
    kubernetes_agent = metadata.get("kubernetes_agent")
    metrics_agent = metadata.get("metrics_agent")
    logs_agent = metadata.get("logs_agent")
    github_agent = metadata.get("github_agent")

    if not all([kubernetes_agent, metrics_agent, logs_agent]):
        logger.warning("Agent instances not found in metadata, creating fallback")
        # Fallback: agents will be created by the wrapper function
        return {
            "investigation_findings": InvestigationFindings(
                correlation_timestamp=datetime.now(timezone.utc).isoformat(),
            ),
            "ooda_phase": "ORIENT",
            "next": "reflector",
            "metadata": {
                **state.get("metadata", {}),
                "investigation_complete": True,
                "investigation_error": "Agent instances not available",
            },
        }

    # Execute agents in parallel
    async def run_agent(agent_name: str, agent_instance):
        logger.info(f"🤖 {agent_name}: Starting investigation")
        thought = f"Alert indicates potential issue. Investigating {agent_name} domain for evidence. Checking Golden Signals..."
        logger.info(f"💭 {agent_name} THOUGHT: {thought}")

        # Add thought to traces
        traces = state.get("thought_traces", {})
        if agent_name not in traces:
            traces[agent_name] = []
        traces[agent_name].append(thought)

        # Create focused state for this agent
        agent_state = {
            **state,
            "current_query": f"As the {agent_name}, investigate: {investigation_query}",
            "thought_traces": traces,
        }

        try:
            # BaseAgentNode uses __call__ which is async, not ainvoke
            result = await agent_instance(agent_state)
            logger.info(f"✅ {agent_name}: Investigation complete")
            return agent_name, result
        except Exception as e:
            logger.error(f"❌ {agent_name}: Investigation failed: {e}")
            return agent_name, {
                "agent_results": {
                    agent_name: f"Error: {str(e)}",
                },
                "thought_traces": traces,
            }

    # Execute agents sequentially to stay within free-tier API rate limits
    logger.info("🔄 Executing agents sequentially (Infra + Code)...")
    
    agent_list = [
        ("kubernetes_agent", kubernetes_agent),
        ("metrics_agent", metrics_agent),
        ("logs_agent", logs_agent),
    ]
    
    # Add GitHub agent if available
    if github_agent:
        agent_list.append(("github_agent", github_agent))
        logger.info("🔄 Including GitHub agent for code change correlation")
    else:
        logger.warning("⚠️ GitHub agent not available - code change correlation disabled")
    
    results = []
    for name, instance in agent_list:
        try:
            res = await run_agent(name, instance)
            results.append(res)
        except Exception as e:
            logger.error(f"Agent {name} raised exception: {e}")
            results.append((name, Exception(str(e))))
    
    # Collect results
    agent_results = state.get("agent_results", {})
    all_traces = state.get("thought_traces", {})

    for name_result in results:
        if not isinstance(name_result, tuple):
            continue
            
        agent_name, result = name_result
        if isinstance(result, Exception):
            logger.error(f"Agent {agent_name} raised exception: {result}")
            agent_results[agent_name] = f"Error: {str(result)}"
        else:
            # Merge agent results
            if isinstance(result, dict):
                agent_results.update(result.get("agent_results", {}))
                all_traces.update(result.get("thought_traces", {}))

    # Extract findings
    infra_findings = {
        "kubernetes": agent_results.get("kubernetes_agent"),
        "metrics": agent_results.get("metrics_agent"),
    }
    logs_findings = agent_results.get("logs_agent")
    code_findings = agent_results.get("github_agent")  # Code change intelligence

    findings = InvestigationFindings(
        infra_findings=infra_findings,
        code_findings=code_findings,
        logs_findings=logs_findings,
        correlation_timestamp=datetime.now(timezone.utc).isoformat(),
    )

    logger.info("✅ InvestigationSwarm: Parallel investigation complete")

    # Update state with findings
    return {
        "investigation_findings": findings,
        "agent_results": agent_results,
        "ooda_phase": "ORIENT",
        "next": "reflector",
        "thought_traces": all_traces,
        "investigation_count": state.get("investigation_count", 0) + 1,
        "metadata": {
            **state.get("metadata", {}),
            "investigation_complete": True,
        },
    }


async def _reflector_node(state: AgentState) -> Dict[str, Any]:
    """
    ReflectorNode: Reviews findings from parallel agents, identifies discrepancies,
    and formulates hypotheses. Implements the ORIENT phase of OODA loop.
    """
    logger.info("🧠 ReflectorNode: Analyzing investigation findings")

    findings = state.get("investigation_findings")
    alert_context = state.get("alert_context")
    agent_results = state.get("agent_results", {})

    if not findings and not agent_results:
        logger.warning("No findings available for reflection")
        return {
            "next": "planner",
            "ooda_phase": "DECIDE",
        }

    # Extract findings from agent results
    infra_findings = agent_results.get("kubernetes_agent") or agent_results.get(
        "metrics_agent"
    )
    logs_findings = agent_results.get("logs_agent")
    code_findings = agent_results.get("github_agent")  # Code change intelligence

    # Detect tool failures (ToolError responses)
    tool_failures = []
    if logs_findings and "TOOL UNAVAILABLE" in str(logs_findings):
        tool_failures.append("Logs")
        logs_findings = None  # Treat as no data
    if infra_findings and "TOOL UNAVAILABLE" in str(infra_findings):
        tool_failures.append("Infrastructure/Metrics")
        infra_findings = None
    if code_findings and "TOOL UNAVAILABLE" in str(code_findings):
        tool_failures.append("GitHub/Code")
        code_findings = None

    # Build tool status message for prompt
    tool_status = ""
    if tool_failures:
        tool_status = f"""
    ⚠️ TOOL UNAVAILABILITY NOTICE:
    The following tools failed after retries and are unavailable: {', '.join(tool_failures)}
    
    CRITICAL INSTRUCTION:
    1. Acknowledge the missing data (e.g., "Unable to access GitHub").
    2. Form a hypothesis based on the REMAINING successful tools.
       Example: "GitHub is down, but Metrics show high latency, so I suspect a resource exhaustion issue unrelated to recent code changes."
    3. Do NOT just stop. Use what you have.
    """
        logger.warning(f"ReflectorNode: Tools unavailable: {tool_failures}")

    # Create LLM for reflection
    # Try to get from metadata, fallback to default
    metadata = state.get("metadata", {})
    llm_provider = metadata.get("llm_provider") or "groq"
    llm = create_llm_with_error_handling(llm_provider)

    # Reflection prompt
    reflection_prompt = f"""
    You are the ReflectorNode in an SRE autonomic system. Your task is to analyze
    findings from parallel investigation agents and identify discrepancies, formulate
    hypotheses, and determine if deeper investigation is needed.
    {tool_status}
    Alert Context:
    {alert_context.model_dump_json() if alert_context else "No alert context"}

    Infrastructure Findings:
    {infra_findings if infra_findings else "No infrastructure findings available"}

    Code Change Findings (GitHub):
    {code_findings if code_findings else "No code change findings available"}

    Logs Findings:
    {logs_findings if logs_findings else "No logs findings available"}

    Analyze these findings and:
    1. Identify any discrepancies between infrastructure and code findings
    2. Formulate a primary hypothesis explaining the incident
    3. Assess confidence level (0.0-1.0)
    4. Determine if deeper investigation is needed
    5. Recommend which agents should investigate further

    Consider Golden Signals:
    - Latency: Is response time degraded?
    - Traffic: Is request volume abnormal?
    - Errors: Are error rates elevated?
    - Saturation: Are resources (CPU, memory, disk) saturated?

    Return your analysis in JSON format matching ReflectorAnalysis schema.
    """

    thought = "Analyzing findings from parallel agents. Looking for discrepancies between infrastructure state and code changes. Formulating hypothesis based on Golden Signals..."
    logger.info(f"💭 ReflectorNode THOUGHT: {thought}")

    traces = state.get("thought_traces", {})
    traces["reflector"] = [thought]

    try:
        # Use structured output for reflection
        from pydantic import BaseModel

        structured_llm = llm.with_structured_output(ReflectorAnalysis)
        analysis = await structured_llm.ainvoke(
            [
                SystemMessage(
                    content="You are an expert SRE analyst. Analyze investigation findings and identify root causes."
                ),
                HumanMessage(content=reflection_prompt),
            ]
        )

        logger.info(f"✅ ReflectorNode: Hypothesis formulated - {analysis.hypothesis}")
        logger.info(f"   Confidence: {analysis.confidence:.2f}")
        logger.info(f"   Discrepancies: {len(analysis.discrepancies)}")

        # Determine next step (configurable investigation depth via MAX_INVESTIGATION_DEPTH)
        max_depth = int(os.getenv("MAX_INVESTIGATION_DEPTH", "3"))
        current_investigation_count = state.get("investigation_count", 0)
        if analysis.requires_deeper_investigation and analysis.recommended_agents and current_investigation_count < max_depth:
            logger.info(
                f"🔄 ReflectorNode: Routing back to agents for deeper investigation"
            )
            return {
                "reflector_analysis": analysis,
                "next": "investigation_swarm",  # Loop back for deeper investigation
                "ooda_phase": "OBSERVE",
                "metadata": {
                    **state.get("metadata", {}),
                    "deeper_investigation_agents": analysis.recommended_agents,
                    "llm_provider": llm_provider,
                },
                "thought_traces": traces,
            }
        else:
            logger.info("➡️ ReflectorNode: Proceeding to planning phase")
            return {
                "reflector_analysis": analysis,
                "next": "planner",
                "ooda_phase": "DECIDE",
                "metadata": {
                    **state.get("metadata", {}),
                    "llm_provider": llm_provider,
                },
                "thought_traces": traces,
            }

    except Exception as e:
        logger.error(f"❌ ReflectorNode: Analysis failed: {e}")
        # Fallback analysis
        fallback_analysis = ReflectorAnalysis(
            hypothesis="Unable to analyze findings automatically. Manual investigation required.",
            confidence=0.0,
            reasoning=f"Error during analysis: {str(e)}",
        )
        return {
            "reflector_analysis": fallback_analysis,
            "next": "planner",
            "ooda_phase": "DECIDE",
            "metadata": {
                **state.get("metadata", {}),
                "llm_provider": llm_provider,
            },
            "thought_traces": traces,
        }


async def _planner_node(state: AgentState) -> Dict[str, Any]:
    """
    PlannerNode: Generates structured RemediationPlan based on reflector analysis.
    Implements the DECIDE phase of OODA loop.
    """
    logger.info("📋 PlannerNode: Generating remediation plan")

    reflector_analysis = state.get("reflector_analysis")
    alert_context = state.get("alert_context")
    agent_results = state.get("agent_results", {})

    if not reflector_analysis:
        logger.warning("No reflector analysis available, creating basic plan")
        reflector_analysis = ReflectorAnalysis(
            hypothesis="Unknown root cause",
            confidence=0.5,
            reasoning="No analysis available",
        )

    # ---------------------------------------------------------
    # 1. Mandatory Runbook Search (RAG)
    # ---------------------------------------------------------
    runbook_content = ""
    source_runbook_url = None
    
    # Try to find the search tool in metadata
    tools = state.get("metadata", {}).get("tools", [])
    search_tool = next((t for t in tools if "search_runbooks" in getattr(t, "name", "")), None)
    
    if search_tool and alert_context:
        logger.info(f"📘 PlannerNode: Searching runbooks for '{alert_context.alert_name}'")
        try:
            # Invoke tool
            if hasattr(search_tool, "ainvoke"):
                search_result = await search_tool.ainvoke({"query": alert_context.alert_name})
            else:
                search_result = search_tool.invoke({"query": alert_context.alert_name})
            
            # Check if relevant
            search_result_str = str(search_result)
            if search_result and "no runbook found" not in search_result_str.lower():
                runbook_content = f"### 📘 RELEVANT RUNBOOK FOUND\n{search_result_str}\n\n"
                runbook_reference = "Start from Runbook"
                logger.info("✅ PlannerNode: Found relevant runbook!")
            else:
                logger.info("planner: No runbook found.")
        except Exception as e:
            logger.warning(f"⚠️ Runbook search failed: {e}")

    # Search memory store for similar past incidents (via MCP if available)
    past_solutions = ""
    try:
        # Try MCP memory server first
        recall_tool = None
        for tool in tools:
            tool_name = getattr(tool, "name", "")
            if "recall_similar_incidents" in tool_name.lower():
                recall_tool = tool
                break

        if recall_tool:
            # Use MCP memory server
            query_text = f"{alert_context.alert_name if alert_context else ''} {reflector_analysis.hypothesis} {reflector_analysis.reasoning}"
            logger.info("🔍 Querying memory via MCP server")
            
            if hasattr(recall_tool, "ainvoke"):
                result = await recall_tool.ainvoke({"query_text": query_text, "limit": 3, "score_threshold": 0.7})
            else:
                result = recall_tool.invoke({"query_text": query_text, "limit": 3, "score_threshold": 0.7})

            # Parse result
            import json
            if isinstance(result, str):
                result_data = json.loads(result)
            elif hasattr(result, "text"):
                result_data = json.loads(result.text)
            else:
                result_data = result

            if "error" not in result_data and result_data.get("results"):
                similar_incidents = result_data.get("results", [])
                # Format for prompt
                if similar_incidents:
                    past_solutions = "## 🧠 Similar Past Incidents and Solutions:\n\n"
                    for i, incident in enumerate(similar_incidents, 1):
                        past_solutions += f"### Incident {i} (Similarity: {incident.get('similarity_score', 0):.2%})\n"
                        past_solutions += f"**ID**: {incident.get('incident_id', 'N/A')}\n\n"
                        past_solutions += f"**Description**: {incident.get('incident_text', 'N/A')}\n\n"
                        if incident.get("metadata", {}).get("resolution"):
                            past_solutions += f"**Resolution**: {incident['metadata']['resolution']}\n\n"
                        past_solutions += "---\n\n"
                    logger.info(f"✅ Found {len(similar_incidents)} similar past incidents via MCP")
        else:
            # Fallback to direct memory store (if available)
            from .memory_store import get_memory_store
            memory = get_memory_store()
            if memory.is_available():
                query_text = f"{alert_context.alert_name if alert_context else ''} {reflector_analysis.hypothesis} {reflector_analysis.reasoning}"
                similar_incidents = memory.search_similar_incidents(query_text, limit=3)
                if similar_incidents:
                    past_solutions = memory.format_similar_incidents_for_prompt(similar_incidents)
                    logger.info(f"✅ Found {len(similar_incidents)} similar past incidents")
    except Exception as e:
        logger.warning(f"⚠️ Memory search failed: {e}")

    # Create LLM for planning
    # Try to get from metadata, fallback to default
    metadata = state.get("metadata", {})
    llm_provider = metadata.get("llm_provider") or "groq"
    llm = create_llm_with_error_handling(llm_provider)

    planning_prompt = f"""
    You are the PlannerNode in an SRE autonomic system. Generate a structured
    remediation plan based on the analysis.

    Hypothesis: {reflector_analysis.hypothesis}
    Confidence: {reflector_analysis.confidence}
    Reasoning: {reflector_analysis.reasoning}

    Alert: {alert_context.alert_name if alert_context else "Unknown"}
    Severity: {alert_context.severity if alert_context else "unknown"}

    {runbook_content}
    
    {past_solutions}

    Generate a remediation plan with:
    1. Specific actions to resolve the issue
    2. Safety checks for each action
    3. Rollback plans
    4. Risk assessment
    5. Verification metrics (Golden Signals)

    CRITICAL INSTRUCTIONS:
    1. IF A RUNBOOK IS FOUND ABOVE: You MUST follow its steps exactly. Do not improvise.
       Set 'source_runbook_url' to the runbook URL if available.
    2. IF NO RUNBOOK: Generate a plan based on first principles and past incidents.
    3. IF PAST INCIDENTS exist: Prioritize their successful resolutions.

    Return plan in JSON format matching RemediationPlan schema.
    """

    thought = f"Formulating remediation plan for hypothesis: {reflector_analysis.hypothesis}. Considering safety, rollback, and verification requirements..."
    logger.info(f"💭 PlannerNode THOUGHT: {thought}")

    traces = state.get("thought_traces", {})
    traces["planner"] = [thought]

    try:
        structured_llm = llm.with_structured_output(RemediationPlan)
        plan = await structured_llm.ainvoke(
            [
                SystemMessage(
                    content="You are an expert SRE planner. Create safe, actionable remediation plans."
                ),
                HumanMessage(content=planning_prompt),
            ]
        )

        # Generate plan ID
        plan.plan_id = f"plan-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        logger.info(f"✅ PlannerNode: Plan generated - {plan.plan_id}")
        logger.info(f"   Actions: {len(plan.actions)}")
        logger.info(f"   Risk Level: {plan.risk_level}")
        logger.info(f"   Requires Approval: {plan.requires_approval}")

        return {
            "remediation_plan": plan,
            "next": "aggregate",
            "ooda_phase": "COMPLETE",
            "approval_status": "PENDING" if plan.requires_approval else "APPROVED",
            "metadata": {
                **state.get("metadata", {}),
                "llm_provider": llm_provider,
            },
            "thought_traces": traces,
        }

    except Exception as e:
        logger.error(f"❌ PlannerNode: Planning failed: {e}")
        # Fallback plan
        fallback_plan = RemediationPlan(
            plan_id=f"plan-fallback-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            hypothesis=reflector_analysis.hypothesis,
            actions=[
                RemediationAction(
                    action_type="escalate",
                    target="manual_review",
                    safety_check="Manual review required due to planning error",
                )
            ],
            estimated_duration="Unknown",
            risk_level="high",
            requires_approval=True,
            verification_metrics=["error_rate", "latency"],
        )
        return {
            "remediation_plan": fallback_plan,
            "next": "aggregate",
            "ooda_phase": "COMPLETE",
            "approval_status": "PENDING",
            "metadata": {
                **state.get("metadata", {}),
                "llm_provider": llm_provider,
            },
            "thought_traces": traces,
        }


        return {
            "next": "aggregate",
            "ooda_phase": "COMPLETE",
        }

    if not alert_context:
        logger.warning("No alert context available for verification")
        return {
            "next": "aggregate",
            "ooda_phase": "COMPLETE",
        }

    # Extract metric information from alert context
    labels = alert_context.labels
    annotations = alert_context.annotations

    # Extract metric name from alert labels or annotations
    # Common patterns: metric name in labels, or constructed from alert name
    original_metric = labels.get("metric") or labels.get("__name__") or "unknown"
    
    # Extract threshold from annotations or labels
    # Prometheus alerts often have threshold in annotations or as a label
    threshold_str = annotations.get("threshold") or labels.get("threshold") or annotations.get("value")
    try:
        threshold = float(threshold_str) if threshold_str else 0.0
    except (ValueError, TypeError):
        logger.warning(f"Could not parse threshold '{threshold_str}', defaulting to 0.0")
        threshold = 0.0

    # Build PromQL query from alert context
    # Try to extract from generator_url or construct from labels
    promql_query = original_metric
    
    # If metric name is unknown, try to construct from alert name
    if original_metric == "unknown":
        alert_name = alert_context.alert_name.lower()
        # Common alert patterns
        if "cpu" in alert_name:
            original_metric = "cpu_usage"
        elif "memory" in alert_name:
            original_metric = "memory_usage"
        elif "latency" in alert_name or "response" in alert_name:
            original_metric = "http_request_duration_seconds"
        elif "error" in alert_name:
            original_metric = "http_requests_total"
        else:
            # Use alert name as metric name
            original_metric = alert_context.alert_name
    
    # Build label filters from alert labels (exclude metadata labels)
    exclude_labels = {"alertname", "severity", "threshold", "__name__"}
    label_filters = []
    for k, v in labels.items():
        if k not in exclude_labels and v:
            label_filters.append(f'{k}="{v}"')
    
    # Construct PromQL query
    if label_filters:
        promql_query = f'{original_metric}{{{",".join(label_filters)}}}'
    else:
        promql_query = original_metric
    

    thought = f"Verifying remediation. Extracted metric: {original_metric}, threshold: {threshold}. Querying Prometheus..."

    traces = state.get("thought_traces", {})

    # Get tools from metadata to access Prometheus MCP tools
    tools = metadata.get("tools", [])
    
    # Find get_metric tool (from Prometheus MCP server)
    get_metric_tool = None
    for tool in tools:
        tool_name = getattr(tool, "name", "")
        # Tool name might be "metrics___get_metric" or just "get_metric"
        if "get_metric" in tool_name.lower() and "range" not in tool_name.lower():
            get_metric_tool = tool
            break

    if not get_metric_tool:
        return {
            "next": "aggregate",
            "ooda_phase": "COMPLETE",
            "thought_traces": traces,
        }

    # Query original metric value at alert time
    original_value = 0.0
    try:
        alert_time = alert_context.starts_at
        query_args = {"query": promql_query}
        if alert_time:
            query_args["time"] = alert_time

        if hasattr(get_metric_tool, "ainvoke"):
            original_result = await get_metric_tool.ainvoke(query_args)
        else:
            original_result = get_metric_tool.invoke(query_args)

        # Parse Prometheus response
        # MCP tool returns TextContent, which is a string containing JSON
        import json
        
        if isinstance(original_result, str):
            original_result = json.loads(original_result)
        elif hasattr(original_result, "text"):
            # Handle TextContent objects
            original_result = json.loads(original_result.text)
        elif hasattr(original_result, "__iter__") and not isinstance(original_result, dict):
            # Handle list of TextContent
            if len(original_result) > 0 and hasattr(original_result[0], "text"):
                original_result = json.loads(original_result[0].text)
        
        # Response format: [{"metric": {...}, "value": [timestamp, "value"]}]
        if isinstance(original_result, list) and len(original_result) > 0:
            # Extract value from Prometheus response
            result_data = original_result[0]
            if isinstance(result_data, dict):
                if "value" in result_data:
                    # Instant query result: ["timestamp", "value"]
                    value_data = result_data["value"]
                    if isinstance(value_data, list) and len(value_data) >= 2:
                        original_value = float(value_data[1])
                elif "values" in result_data:
                    # Range query result: use last value
                    values = result_data["values"]
                    if values and isinstance(values[-1], list) and len(values[-1]) >= 2:
                        original_value = float(values[-1][1])


    except Exception as e:
        original_value = 0.0

    # Get configurable wait time (default 60 seconds)
    wait_seconds = int(os.getenv("VERIFICATION_WAIT_SECONDS", "60"))
    
    thought_wait = f"Waiting {wait_seconds} seconds for remediation to take effect, then re-querying metric..."

    # Wait for remediation to take effect
    await asyncio.sleep(wait_seconds)

    # Re-query current metric value
    current_value = 0.0
    try:
        if hasattr(get_metric_tool, "ainvoke"):
            current_result = await get_metric_tool.ainvoke({"query": promql_query})
        else:
            current_result = get_metric_tool.invoke({"query": promql_query})

        # Parse Prometheus response
        import json
        
        if isinstance(current_result, str):
            current_result = json.loads(current_result)
        elif hasattr(current_result, "text"):
            # Handle TextContent objects
            current_result = json.loads(current_result.text)
        elif hasattr(current_result, "__iter__") and not isinstance(current_result, dict):
            # Handle list of TextContent
            if len(current_result) > 0 and hasattr(current_result[0], "text"):
                current_result = json.loads(current_result[0].text)
        
        if isinstance(current_result, list) and len(current_result) > 0:
            result_data = current_result[0]
            if isinstance(result_data, dict):
                if "value" in result_data:
                    value_data = result_data["value"]
                    if isinstance(value_data, list) and len(value_data) >= 2:
                        current_value = float(value_data[1])
                elif "values" in result_data:
                    values = result_data["values"]
                    if values and isinstance(values[-1], list) and len(values[-1]) >= 2:
                        current_value = float(values[-1][1])


    except Exception as e:
        current_value = original_value  # Fallback to original if query fails

    # Calculate improvement
    if original_value > 0:
        improvement = ((original_value - current_value) / original_value) * 100
    else:
        improvement = 0.0

    # Determine status based on threshold comparison
    if current_value < threshold:
        status = "RESOLVED"
    else:
        status = "FAILED"

    # Query Golden Signals from Prometheus
    golden_signals = {}
    try:
        # Find get_golden_signals tool
        get_golden_signals_tool = None
        for tool in tools:
            tool_name = getattr(tool, "name", "")
            if "get_golden_signals" in tool_name.lower():
                get_golden_signals_tool = tool
                break

        if get_golden_signals_tool:
            service = labels.get("service") or labels.get("pod", "").split("-")[0] if labels.get("pod") else ""
            namespace = labels.get("namespace", "default")
            
            gs_args = {"service": service, "namespace": namespace}
            if hasattr(get_golden_signals_tool, "ainvoke"):
                gs_result = await get_golden_signals_tool.ainvoke(gs_args)
            else:
                gs_result = get_golden_signals_tool.invoke(gs_args)

            # Parse Golden Signals response
            if isinstance(gs_result, str):
                import json
                gs_result = json.loads(gs_result)

            if isinstance(gs_result, dict):
                # Extract status for each signal
                for signal_name, signal_data in gs_result.items():
                    if isinstance(signal_data, dict) and "value" in signal_data:
                        # Determine status based on value
                        signal_value = signal_data.get("value", [])
                        if isinstance(signal_value, list) and len(signal_value) > 0:
                            value = float(signal_value[0][1]) if isinstance(signal_value[0], list) else float(signal_value[0])
                            # Golden signal thresholds — configurable via env
                            _gs_latency = float(os.getenv("GS_LATENCY_THRESHOLD", "1.0"))
                            _gs_error = float(os.getenv("GS_ERROR_THRESHOLD", "0.01"))
                            _gs_saturation = float(os.getenv("GS_SATURATION_THRESHOLD", "0.8"))
                            if signal_name == "latency":
                                golden_signals[signal_name] = "normal" if value < _gs_latency else "degraded"
                            elif signal_name == "errors":
                                golden_signals[signal_name] = "normal" if value < _gs_error else "elevated"
                            elif signal_name == "saturation":
                                golden_signals[signal_name] = "normal" if value < _gs_saturation else "high"
                            else:
                                golden_signals[signal_name] = "normal"
                    else:
                        golden_signals[signal_name] = "unknown"
        else:
            logger.warning("get_golden_signals tool not found, using status-based defaults")
            # Fallback: determine Golden Signals based on verification status
            golden_signals = {
                "latency": "normal" if status == "RESOLVED" else "degraded",
                "traffic": "normal",
                "errors": "normal" if status == "RESOLVED" else "elevated",
                "saturation": "normal" if status == "RESOLVED" else "high",
            }
    except Exception as e:
        # Fallback Golden Signals
        golden_signals = {
            "latency": "normal" if status == "RESOLVED" else "degraded",
            "traffic": "normal",
            "errors": "normal" if status == "RESOLVED" else "elevated",
            "saturation": "normal" if status == "RESOLVED" else "high",
        }

    verification = VerificationResult(
        status=status,
        original_metric=original_metric,
        original_value=original_value,
        current_value=current_value,
        threshold=threshold,
        improvement_percentage=improvement,
        golden_signals_status=golden_signals,
        verification_timestamp=datetime.now(timezone.utc).isoformat(),
        next_steps=[] if status == "RESOLVED" else ["Monitor for 10 minutes", "Consider additional remediation"],
    )


    return {
        "next": "aggregate",
        "ooda_phase": "COMPLETE",
        "thought_traces": traces,
    }


def build_multi_agent_graph(
    tools: List[BaseTool],
    llm_provider: str = "ollama",
    export_graph: bool = False,
    graph_output_path: str = "./graph_architecture.md",
    **llm_kwargs,
) -> StateGraph:
    """
    Build the multi-agent collaboration graph implementing OODA Loop pattern.
    
    Architecture:
    - OBSERVE: InvestigationSwarm (parallel agents)
    - ORIENT: ReflectorNode (analysis and hypothesis)
    - DECIDE: PlannerNode (remediation plan)
    - ACT: PolicyGateNode -> ExecutorNode
    
    Args:
        tools: List of all available tools
        llm_provider: LLM provider to use
        export_graph: Whether to export the graph as a Mermaid diagram
        graph_output_path: Path to save the exported Mermaid diagram
        **llm_kwargs: Additional arguments for LLM

    Returns:
        Compiled StateGraph for multi-agent collaboration
    """
    logger.info("Building OODA Loop-based multi-agent collaboration graph")

    # Create the state graph
    workflow = StateGraph(AgentState)

    # Create supervisor (for backward compatibility and routing)
    supervisor = SupervisorAgent(
        llm_provider=llm_provider, **llm_kwargs
    )

    # Create agent nodes with filtered tools and metadata from constants
    kubernetes_agent = create_kubernetes_agent(
        tools,
        agent_metadata=SREConstants.agents.agents["kubernetes"],
        llm_provider=llm_provider,
        **llm_kwargs,
    )
    logs_agent = create_logs_agent(
        tools,
        agent_metadata=SREConstants.agents.agents["logs"],
        llm_provider=llm_provider,
        **llm_kwargs,
    )
    metrics_agent = create_metrics_agent(
        tools,
        agent_metadata=SREConstants.agents.agents["metrics"],
        llm_provider=llm_provider,
        **llm_kwargs,
    )
    runbooks_agent = create_runbooks_agent(
        tools,
        agent_metadata=SREConstants.agents.agents["runbooks"],
        llm_provider=llm_provider,
        **llm_kwargs,
    )
    github_agent = create_github_agent(
        tools,
        agent_metadata=SREConstants.agents.agents["github"],
        llm_provider=llm_provider,
        **llm_kwargs,
    )

    # Store agents and tools in a way that nodes can access them
    # We'll pass them via metadata
    async def investigation_swarm_with_agents(state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Wrapper that provides agents to investigation swarm."""
        # Store agents and tools in metadata for access
        state_with_agents = {
            **state,
            "metadata": {
                **state.get("metadata", {}),
                "kubernetes_agent": kubernetes_agent,
                "metrics_agent": metrics_agent,
                "logs_agent": logs_agent,
                "github_agent": github_agent,
                "tools": tools,  # Pass tools for executor node
                "llm_provider": llm_provider,
            },
        }
        if config:
            result = await _investigation_swarm(state_with_agents, config=config)
        else:
            result = await _investigation_swarm(state_with_agents)
        # Preserve metadata with agents and tools for future nodes
        result["metadata"] = {
            **result.get("metadata", {}),
            "kubernetes_agent": kubernetes_agent,
            "metrics_agent": metrics_agent,
            "logs_agent": logs_agent,
            "github_agent": github_agent,
            "tools": tools,  # Preserve tools for executor
            "llm_provider": llm_provider,
        }
        return result

    # Add nodes to the graph
    workflow.add_node("prepare", _prepare_initial_state)
    workflow.add_node("supervisor", supervisor.route)
    
    # OODA Loop nodes
    workflow.add_node("investigation_swarm", investigation_swarm_with_agents)
    workflow.add_node("reflector", _reflector_node)
    workflow.add_node("planner", _planner_node)
    
    # Legacy agent nodes (for backward compatibility and direct invocation)
    workflow.add_node("kubernetes_agent", kubernetes_agent)
    workflow.add_node("logs_agent", logs_agent)
    workflow.add_node("metrics_agent", metrics_agent)
    workflow.add_node("runbooks_agent", runbooks_agent)
    
    # Aggregation node
    workflow.add_node("aggregate", supervisor.aggregate_responses)

    # Set entry point
    workflow.set_entry_point("prepare")

    # Route from prepare: use supervisor for planning (if enabled) or go directly to swarm
    if os.getenv("ENABLE_SUPERVISOR_ROUTING", "false").lower() in ("true", "1", "yes"):
        workflow.add_edge("prepare", "supervisor")
    else:
        workflow.add_edge("prepare", "investigation_swarm")

    # Add conditional edges from supervisor
    workflow.add_conditional_edges(
        "supervisor",
        _route_supervisor,
        {
            "investigation_swarm": "investigation_swarm",
            "reflector": "reflector",
            "planner": "planner",
            "aggregate": "aggregate",
            # Legacy routing for backward compatibility
            "kubernetes_agent": "kubernetes_agent",
            "logs_agent": "logs_agent",
            "metrics_agent": "metrics_agent",
            "runbooks_agent": "runbooks_agent",
        },
    )

    # Policy Gate Node (Phase C Implementation)

    # OODA Loop flow
    workflow.add_edge("investigation_swarm", "reflector")
    
    # Reflector can loop back to investigation or proceed to planner
    def _route_reflector(state: AgentState) -> str:
        """Route from reflector based on analysis."""
        next_node = state.get("next", "planner")
        return next_node
    
    workflow.add_conditional_edges(
        "reflector",
        _route_reflector,
        {
            "investigation_swarm": "investigation_swarm",
            "planner": "planner",
        },
    )
    
    # Planner -> Aggregate
    workflow.add_edge("planner", "aggregate")

    # Legacy edges from agents back to supervisor (for backward compatibility)
    workflow.add_edge("kubernetes_agent", "supervisor")
    workflow.add_edge("logs_agent", "supervisor")
    workflow.add_edge("metrics_agent", "supervisor")
    workflow.add_edge("runbooks_agent", "supervisor")

    # Add edge from aggregate to END
    workflow.add_edge("aggregate", END)

    # Compile the graph
    compiled_graph = workflow.compile()

    # Export graph visualization if requested
    if export_graph:
        try:
            # Create docs directory if it doesn't exist
            from pathlib import Path
            output_path = Path(graph_output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Get the Mermaid representation of the graph
            mermaid_diagram = compiled_graph.get_graph().draw_mermaid()
            
            # Save to file
            with open(graph_output_path, "w") as f:
                f.write("# SRE Agent Architecture (OODA Loop)\n\n")
                f.write("## OOD Flow:\n")
                f.write("- **OBSERVE**: investigation_swarm (parallel agents)\n")
                f.write("- **ORIENT**: reflector (analysis & hypothesis)\n")
                f.write("- **DECIDE**: planner (remediation plan)\n\n")
                f.write("```mermaid\n")
                f.write(mermaid_diagram)
                f.write("\n```\n")
            
            logger.info(f"Graph architecture (Mermaid) exported to: {graph_output_path}")
            print(f"✅ Graph architecture (Mermaid diagram) exported to: {graph_output_path}")
        except Exception as e:
            logger.error(f"Failed to export graph: {e}")
            print(f"❌ Failed to export graph: {e}")

    logger.info("OODA Loop-based multi-agent collaboration graph built successfully")
    return compiled_graph
