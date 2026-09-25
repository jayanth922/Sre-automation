#!/usr/bin/env python3

"""
Context Builder for SRE Agent - Alert Enrichment

Enriches Prometheus alerts with additional context before triggering
the investigation graph. Queries infrastructure and runbooks to provide
comprehensive context.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from langchain_core.tools import BaseTool

from .agent_state import AlertContext
from .runbook_brief import (
    parse_search_results,
    render_runbook_brief,
    select_runbook,
    summarize_search_results,
)

# Enough to show the pod phase, its container statuses and the last
# termination reason; far short of a full `-o json` manifest.
DEFAULT_POD_STATUS_MAX_CHARS = 2000

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


def _pod_status_max_chars() -> int:
    raw = os.getenv("POD_STATUS_CONTEXT_MAX_CHARS", "").strip()
    if not raw:
        return DEFAULT_POD_STATUS_MAX_CHARS
    try:
        return max(int(raw), 200)
    except ValueError:
        return DEFAULT_POD_STATUS_MAX_CHARS


def _bounded(text: str, max_chars: int) -> str:
    """Keep both ends of an oversized payload rather than only the head."""
    if len(text) <= max_chars:
        return text
    marker = "\n… [trimmed to fit the alert context] …\n"
    room = max(max_chars - len(marker), 200)
    head = room // 2
    return text[:head] + marker + text[-(room - head):]


class ContextBuilder:
    """Builds enriched context for alerts before investigation."""

    def __init__(self, tools: List[BaseTool]):
        """
        Initialize context builder with available tools.

        Args:
            tools: List of MCP tools available for context enrichment
        """
        self.tools = tools
        logger.info(f"ContextBuilder initialized with {len(tools)} tools")

    def _find_tool(self, tool_name: str) -> Optional[BaseTool]:
        """
        Find a tool by name (handles domain prefixes).

        Args:
            tool_name: Tool name (with or without domain prefix)

        Returns:
            Tool instance or None if not found
        """
        for tool in self.tools:
            tool_base_name = (
                getattr(tool, "name", "").split("___")[-1]
                if "___" in getattr(tool, "name", "")
                else getattr(tool, "name", "")
            )
            if tool_base_name == tool_name:
                return tool
        return None

    async def enrich_alert_context(self, alert: Dict[str, Any]) -> AlertContext:
        """
        Enrich alert with additional context from infrastructure and runbooks.

        Args:
            alert: Prometheus alert payload (single alert object)

        Returns:
            Enriched AlertContext with additional information
        """
        logger.info("🔍 ContextBuilder: Enriching alert context")

        # Extract basic alert information
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        alert_name = labels.get("alertname", "UnknownAlert")
        pod_name = labels.get("pod")
        namespace = labels.get("namespace", "default")
        severity = labels.get("severity", "warning").lower()

        logger.info(
            f"🔍 ContextBuilder: Alert={alert_name}, Pod={pod_name}, Namespace={namespace}"
        )

        # Step 1: Check pod status (if pod is specified)
        pod_status_info = None
        if pod_name:
            pod_status_tool = self._find_tool("get_pod_status")
            if pod_status_tool:
                try:
                    logger.info(f"🔍 ContextBuilder: Checking pod status for {pod_name}")
                    pod_args = {
                        "pod_name": pod_name,
                        "namespace": namespace,
                    }
                    if hasattr(pod_status_tool, "ainvoke"):
                        pod_result = await pod_status_tool.ainvoke(pod_args)
                    else:
                        pod_result = pod_status_tool.invoke(pod_args)

                    # Head-and-tail, not a head slice: a pod status payload
                    # carries its phase up front and the container statuses,
                    # restart counts and last-termination reason at the end,
                    # and the tail is the half that explains a crash loop.
                    pod_status_info = _bounded(str(pod_result), _pod_status_max_chars())
                    logger.info(f"✅ ContextBuilder: Pod status retrieved")
                except Exception as e:
                    logger.warning(f"⚠️ ContextBuilder: Failed to get pod status: {e}")
                    pod_status_info = f"Error retrieving pod status: {str(e)}"
            else:
                logger.warning("⚠️ ContextBuilder: get_pod_status tool not found")

        # Step 2: Search for relevant runbooks, then fetch the winner's body.
        runbook_info = await self.build_runbook_context(
            alert_name=alert_name,
            severity=severity,
            service=labels.get("service") or labels.get("job") or "",
        )

        # Step 3: Enrich annotations with context
        enriched_annotations = dict(annotations)
        if pod_status_info:
            enriched_annotations["pod_status_context"] = pod_status_info
        if runbook_info:
            enriched_annotations["runbook_context"] = runbook_info

        # Create enriched AlertContext
        enriched_context = AlertContext(
            alert_name=alert_name,
            severity=severity,  # type: ignore
            labels=labels,
            annotations=enriched_annotations,
            starts_at=alert.get("startsAt"),
            generator_url=alert.get("generatorURL"),
        )

        logger.info(f"✅ ContextBuilder: Context enrichment complete")

        return enriched_context

    async def _call_tool(self, tool: BaseTool, args: Dict[str, Any]) -> Any:
        if hasattr(tool, "ainvoke"):
            return await tool.ainvoke(args)
        return tool.invoke(args)

    async def build_runbook_context(
        self, *, alert_name: str, severity: str, service: str = ""
    ) -> Optional[str]:
        """Resolve this alert to an executable runbook procedure.

        Two calls, not one. ``search_runbooks`` returns properties and a
        320-character keyword excerpt per hit — enough to identify a runbook,
        never enough to follow it. The steps live in the page body, so the
        winning hit is fetched with ``get_runbook_content`` and rendered
        remediation-first. Skipping the second call is what left the agent to
        re-derive, from logs, a fix that was already written down.

        Every failure degrades rather than raises: no runbook is a worse
        investigation, but an exception here would mean no investigation.
        """
        search_tool = self._find_tool("search_runbooks")
        if not search_tool:
            logger.warning("⚠️ ContextBuilder: search_runbooks tool not found")
            return None

        logger.info(f"🔍 ContextBuilder: Searching runbooks for alert '{alert_name}'")
        base_args: Dict[str, Any] = {
            "incident_type": self._map_alert_to_incident_type(alert_name),
            "keyword": alert_name,
            "severity": severity,
        }
        # `alert_name` and `service` are the two properties the Notion corpus
        # is actually indexed on, so they materially improve the match. They
        # are not in every runbook backend's signature, hence the retry: a
        # narrower search beats a rejected one.
        rich_args = {**base_args, "alert_name": alert_name, "service": service}
        raw = None
        for args in (rich_args, base_args):
            try:
                raw = await self._call_tool(search_tool, args)
                break
            except Exception as e:
                logger.warning(
                    f"⚠️ ContextBuilder: search_runbooks({sorted(args)}) failed: {e}"
                )
        if raw is None:
            return None

        results = parse_search_results(raw)
        if not results:
            logger.warning(
                f"⚠️ ContextBuilder: No runbook matched alert '{alert_name}'"
            )
            return None

        chosen = select_runbook(results, alert_name=alert_name, service=service)
        if chosen is None:
            return None

        content = await self._fetch_runbook_content(chosen)
        if content:
            brief = render_runbook_brief(chosen, content)
            logger.info(
                "✅ ContextBuilder: Runbook '%s' resolved to a %d-char brief",
                chosen.get("title", "?"),
                len(brief),
            )
            return brief

        # The body could not be fetched. Name the candidates and how to read
        # them rather than passing a keyword excerpt off as the procedure.
        logger.warning(
            "⚠️ ContextBuilder: Could not fetch body for runbook '%s'; "
            "falling back to the search listing",
            chosen.get("title", "?"),
        )
        return summarize_search_results(results) or render_runbook_brief(chosen)

    async def _fetch_runbook_content(self, runbook: Dict[str, Any]) -> str:
        """Fetch the runbook's page body, by id and then by title."""
        content_tool = self._find_tool("get_runbook_content")
        if not content_tool:
            logger.warning("⚠️ ContextBuilder: get_runbook_content tool not found")
            return ""

        # The Notion server resolves a page id, a title, or a slug, so a
        # corpus whose search hits carry no usable id still resolves.
        for key in ("runbook_id", "title"):
            page_id = str(runbook.get(key) or "").strip()
            if not page_id:
                continue
            try:
                raw = await self._call_tool(content_tool, {"page_id": page_id})
            except Exception as e:
                logger.warning(
                    f"⚠️ ContextBuilder: get_runbook_content({key}) failed: {e}"
                )
                continue
            for hit in parse_search_results(raw):
                content = str(hit.get("content") or hit.get("section") or "").strip()
                if content:
                    return content
        return ""

    def _map_alert_to_incident_type(self, alert_name: str) -> str:
        """
        Map alert name to incident type for runbook search.

        Args:
            alert_name: Name of the alert

        Returns:
            Incident type (performance, availability, security, deployment)
        """
        alert_lower = alert_name.lower()

        if any(keyword in alert_lower for keyword in ["cpu", "memory", "latency", "response"]):
            return "performance"
        elif any(keyword in alert_lower for keyword in ["down", "unavailable", "crash"]):
            return "availability"
        elif any(keyword in alert_lower for keyword in ["security", "vulnerability", "breach"]):
            return "security"
        elif any(keyword in alert_lower for keyword in ["deploy", "rollout", "update"]):
            return "deployment"
        else:
            return "performance"  # Default


async def resolve_runbook_context(
    tools: List[BaseTool],
    *,
    alert_name: str,
    severity: str = "warning",
    labels: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve an alert to a runbook brief, for callers that build their own
    ``AlertContext``.

    The SaaS investigation path constructs ``AlertContext`` directly from the
    incident row and never instantiates :class:`ContextBuilder`, so until this
    existed the runbook reached the agent in *local fallback mode only* — that
    is, never in production. Whether an alert arrives with its runbook must not
    depend on which code path happened to build it.
    """
    labels = labels or {}
    return await ContextBuilder(tools).build_runbook_context(
        alert_name=alert_name,
        severity=severity,
        service=str(labels.get("service") or labels.get("job") or ""),
    )
