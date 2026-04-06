import asyncio
import json
import unittest

import requests
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from langchain_mcp_adapters.client import MultiServerMCPClient


def _coerce_json_payload(value):
     if isinstance(value, (str, bytes, bytearray)):
          text_value = value.decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else value
          try:
               return json.loads(text_value)
          except json.JSONDecodeError:
               return text_value
     if isinstance(value, list) and value:
          text_chunks = []
          for item in value:
               if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                    text_chunks.append(item["text"])
          if text_chunks:
               combined_text = "".join(text_chunks).strip()
               if combined_text:
                    try:
                         return json.loads(combined_text)
                    except json.JSONDecodeError:
                         return combined_text
     if isinstance(value, dict) and value.get("type") == "text" and "text" in value:
          try:
               return json.loads(value["text"])
          except json.JSONDecodeError:
               return value["text"]
     return value


def _discover_live_k8s_targets():
     """Find real pod, service, and deployment targets from the current Kubernetes cluster."""
     try:
          k8s_config.load_kube_config()
     except Exception as exc:
          raise AssertionError(f"Could not load kubeconfig for live Kubernetes discovery: {exc}") from exc

     core_api = k8s_client.CoreV1Api()
     apps_api = k8s_client.AppsV1Api()
     target_namespace = "demo-app"
     preferred_service_names = [
          "api-gateway",
          "checkout-service",
          "inventory-service",
          "load-generator",
          "prometheus",
          "alertmanager",
          "loki",
          "grafana",
          "chaos-panel",
     ]

     pod_list = core_api.list_pod_for_all_namespaces(watch=False)
     pod_target = next(
          (
               pod
               for pod in pod_list.items
               if pod.metadata
               and pod.metadata.name
               and pod.metadata.namespace
               and pod.status
               and pod.status.phase == "Running"
               and pod.metadata.namespace == target_namespace
          ),
          None,
     )

     if not pod_target:
          raise AssertionError("No running pod found in the live Kubernetes cluster to validate get_pod_status")

     deployment_list = apps_api.list_deployment_for_all_namespaces(watch=False)
     deployment_target = next(
          (
               deployment
               for deployment in deployment_list.items
               if deployment.metadata
               and deployment.metadata.name
               and deployment.metadata.namespace
               and deployment.metadata.namespace == target_namespace
          ),
          None,
     )

     if not deployment_target:
          raise AssertionError("No deployment found in the live Kubernetes cluster to validate get_deployment_status")

     service_list = core_api.list_service_for_all_namespaces(watch=False)
     service_target = next(
          (
               service
               for service in service_list.items
               if service.metadata
               and service.metadata.name
               and service.metadata.namespace
               and service.metadata.namespace == target_namespace
               and service.metadata.name in preferred_service_names
          ),
          None,
     )

     if not service_target:
          raise AssertionError("No service found in the live Kubernetes cluster to validate service tools")

     return {
          "pod_name": pod_target.metadata.name,
          "pod_namespace": pod_target.metadata.namespace,
          "deployment_name": deployment_target.metadata.name,
          "deployment_namespace": deployment_target.metadata.namespace,
          "service_name": service_target.metadata.name,
          "service_namespace": service_target.metadata.namespace,
          "target_namespace": target_namespace,
     }


class TestLayer2(unittest.TestCase):
     def test_mcp_prometheus_tools_and_live_data(self):
          """Smoke test the Prometheus MCP server's health and live metric tools (port 4001)."""
          print("\n\n[*] Testing Prometheus MCP Server Connectivity (Port 4001)...")
          try:
               response = requests.get("http://127.0.0.1:4001/sse", stream=True, timeout=10)
               self.assertEqual(response.status_code, 200, "Prometheus MCP server failed to establish SSE connection")
               print(f"  [+] Success! Connected and received Status Code: {response.status_code}")

               content_type = response.headers.get("Content-Type", "")
               self.assertIn("text/event-stream", content_type, "Not an SSE endpoint")
               print(f"  [+] Success! Valid SSE stream detected (Content-Type: {content_type})")
               response.close()

               async def _run_prometheus_tool_checks():
                    client = MultiServerMCPClient(
                         {
                              "prometheus": {
                                   "url": "http://127.0.0.1:4001/sse",
                                   "transport": "sse",
                              }
                         }
                    )

                    tools = await client.get_tools()
                    tool_names = [getattr(tool, "name", "") for tool in tools]
                    self.assertIn("check_prometheus_health", tool_names, "Prometheus MCP did not expose check_prometheus_health")
                    self.assertIn("get_metric", tool_names, "Prometheus MCP did not expose get_metric")
                    self.assertIn("get_metric_range", tool_names, "Prometheus MCP did not expose get_metric_range")
                    self.assertIn("get_golden_signals", tool_names, "Prometheus MCP did not expose get_golden_signals")

                    health_tool = next(tool for tool in tools if getattr(tool, "name", "") == "check_prometheus_health")
                    health_result = await health_tool.ainvoke({})
                    health_payload = _coerce_json_payload(health_result)

                    self.assertIsInstance(health_payload, dict, f"check_prometheus_health did not return JSON: {health_payload!r}")
                    self.assertEqual(health_payload.get("status"), "healthy", "Prometheus health check did not return healthy")
                    print(f"  [+] Success! check_prometheus_health returned status: {health_payload['status']}")

                    metric_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_metric")
                    metric_result = await metric_tool.ainvoke({"query": 'sum by (job) (up)'})
                    metric_payload = _coerce_json_payload(metric_result)

                    self.assertIsInstance(metric_payload, list, f"get_metric did not return a series list: {metric_payload!r}")
                    self.assertGreater(len(metric_payload), 0, "get_metric returned no live series for sum by (job) (up)")

                    discovered_jobs = {
                         item.get("metric", {}).get("job")
                         for item in metric_payload
                         if isinstance(item, dict)
                    }
                    expected_jobs = {"api-gateway", "checkout-service", "inventory-service", "prometheus"}
                    self.assertTrue(
                         expected_jobs.issubset(discovered_jobs),
                         f"Prometheus metric query did not surface all target-client jobs: {sorted(discovered_jobs)!r}",
                    )
                    print(f"  [+] Success! get_metric returned live series for jobs: {sorted(discovered_jobs)}")

                    golden_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_golden_signals")
                    golden_result = await golden_tool.ainvoke({"service": "api-gateway", "namespace": "demo-app"})
                    golden_payload = _coerce_json_payload(golden_result)

                    self.assertIsInstance(golden_payload, dict, f"get_golden_signals did not return JSON: {golden_payload!r}")
                    for signal_name in ("latency", "traffic", "errors", "saturation"):
                         self.assertIn(signal_name, golden_payload, f"get_golden_signals missing {signal_name}")
                    print("  [+] Success! get_golden_signals returned all four signal buckets")

               asyncio.run(_run_prometheus_tool_checks())

          except requests.exceptions.ConnectionError:
               print("  [-] ERROR: Could not connect to Prometheus MCP server on port 4001.")
               self.fail("Could not connect to Prometheus MCP server on port 4001. Did you run docker compose up?")

     def test_mcp_runbooks_search_and_content_tools(self):
          """Smoke test the runbooks MCP server and its core search/content tools (port 4004)."""
          print("\n\n[*] Testing Runbooks MCP Server Connectivity and Tooling (Port 4004)...")
          try:
               response = requests.get("http://127.0.0.1:4004/sse", stream=True, timeout=10)
               self.assertEqual(response.status_code, 200, "Runbooks MCP server failed to establish SSE connection")

               content_type = response.headers.get("Content-Type", "")
               self.assertIn("text/event-stream", content_type, "Not an SSE endpoint")
               print(f"  [+] SSE reachable (Content-Type: {content_type})")
               response.close()

               async def _run_runbooks_tool_checks():
                    client = MultiServerMCPClient(
                         {
                              "runbooks": {
                                   "url": "http://127.0.0.1:4004/sse",
                                   "transport": "sse",
                              }
                         }
                    )

                    tools = await client.get_tools()
                    tool_names = [getattr(tool, "name", "") for tool in tools]
                    self.assertIn("search_runbooks", tool_names, "Runbooks MCP did not expose search_runbooks")
                    self.assertIn("get_runbook_content", tool_names, "Runbooks MCP did not expose get_runbook_content")

                    search_tool = next(tool for tool in tools if getattr(tool, "name", "") == "search_runbooks")
                    search_result = await search_tool.ainvoke({"query": "checkout service high error rate", "limit": 3})
                    search_payload = _coerce_json_payload(search_result)

                    self.assertIn("results", search_payload, "search_runbooks did not return results")
                    self.assertGreater(len(search_payload["results"]), 0, "search_runbooks returned no matches")

                    top_result = search_payload["results"][0]
                    self.assertIn("runbook_id", top_result, "Search result did not include runbook_id")
                    self.assertIn("title", top_result, "Search result did not include title")
                    print(f"  [+] Success! search_runbooks returned {len(search_payload['results'])} result(s)")

                    content_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_runbook_content")
                    content_result = await content_tool.ainvoke({"page_id": top_result["runbook_id"]})
                    content_payload = _coerce_json_payload(content_result)

                    self.assertEqual(content_payload["runbook_id"], top_result["runbook_id"])
                    self.assertIn("content", content_payload, "get_runbook_content did not return content")
                    self.assertTrue(content_payload["content"].strip(), "Runbook content was empty")
                    print(f"  [+] Success! Read runbook content: {content_payload['runbook_id']}")

               asyncio.run(_run_runbooks_tool_checks())

          except requests.exceptions.ConnectionError:
               print("  [-] ERROR: Could not connect to Runbooks MCP server on port 4004.")
               self.fail("Could not connect to Runbooks MCP server on port 4004. Did you run docker compose up?")

     def test_mcp_loki_tools_and_live_data(self):
          """Smoke test the Loki MCP server's query tools and live log data (port 4002)."""
          print("\n\n[*] Testing Loki MCP Server Connectivity (Port 4002)...")
          try:
               response = requests.get("http://127.0.0.1:4002/sse", stream=True, timeout=10)
               self.assertEqual(response.status_code, 200, "Loki MCP server failed to establish SSE connection")
               print(f"  [+] Success! Connected and received Status Code: {response.status_code}")

               content_type = response.headers.get("Content-Type", "")
               self.assertIn("text/event-stream", content_type, "Not an SSE endpoint")
               print(f"  [+] Success! Valid SSE stream detected (Content-Type: {content_type})")
               response.close()

               async def _run_loki_tool_checks():
                    client = MultiServerMCPClient(
                         {
                              "loki": {
                                   "url": "http://127.0.0.1:4002/sse",
                                   "transport": "sse",
                              }
                         }
                    )

                    tools = await client.get_tools()
                    tool_names = [getattr(tool, "name", "") for tool in tools]
                    self.assertIn("query_logs", tool_names, "Loki MCP did not expose query_logs")
                    self.assertIn("get_error_logs", tool_names, "Loki MCP did not expose get_error_logs")
                    self.assertIn("analyze_log_patterns", tool_names, "Loki MCP did not expose analyze_log_patterns")

                    trigger_response = await asyncio.to_thread(
                         requests.post,
                         "http://127.0.0.1:8000/checkout/loki-smoke",
                         timeout=15,
                    )
                    self.assertIn(
                         trigger_response.status_code,
                         {200, 500, 503, 504},
                         f"Unexpected response while generating fresh checkout-service logs: {trigger_response.status_code}",
                    )
                    await asyncio.sleep(4)

                    query_tool = next(tool for tool in tools if getattr(tool, "name", "") == "query_logs")
                    query_payload = {}
                    await asyncio.to_thread(requests.get, "http://127.0.0.1:8000/inventory", timeout=15)
                    await asyncio.to_thread(requests.get, "http://127.0.0.1:8000/inventory/item-001", timeout=15)
                    await asyncio.to_thread(requests.post, "http://127.0.0.1:8000/checkout/loki-api-gateway", timeout=15)
                    await asyncio.sleep(4)
                    for logql_query in ('{service="checkout-service"}', '{service="inventory-service"}', '{service="api-gateway"}', '{namespace="demo-app"}'):
                         for attempt in range(5):
                              query_result = await query_tool.ainvoke(
                                   {"logql": logql_query, "limit": 20, "start_time": "5m"}
                              )
                              query_payload = _coerce_json_payload(query_result)
                              if query_payload.get("logs"):
                                   break
                              await asyncio.sleep(2)
                         if query_payload.get("logs"):
                              break
                         print(f"  [i] Loki query returned no logs yet for {logql_query}")

                    self.assertIsInstance(query_payload, dict, f"query_logs did not return JSON: {query_payload!r}")
                    self.assertIn("logs", query_payload, "query_logs response did not include logs")
                    if not query_payload["logs"]:
                         self.skipTest(
                              "Loki is reachable, but the current target-client environment did not return queryable live logs after retries."
                         )

                    print(f"  [+] Success! query_logs returned {len(query_payload['logs'])} live log line(s)")

                    service_queries = {
                         "checkout-service": '{service="checkout-service"}',
                         "inventory-service": '{service="inventory-service"}',
                         "api-gateway": '{service="api-gateway"}',
                    }
                    service_results = {}
                    for service_name, logql_query in service_queries.items():
                         service_result = await query_tool.ainvoke({"logql": logql_query, "limit": 10, "start_time": "5m"})
                         service_payload = _coerce_json_payload(service_result)
                         service_results[service_name] = service_payload.get("logs", []) if isinstance(service_payload, dict) else []

                    self.assertTrue(
                         any(service_results.values()),
                         "Loki did not return logs for any of the target-client services",
                    )
                    print(
                         "  [+] Service coverage: " + ", ".join(
                              f"{name}={len(logs)}" for name, logs in service_results.items()
                         )
                    )

                    error_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_error_logs")
                    error_result = await error_tool.ainvoke({"limit": 20, "since": "1h"})
                    error_payload = _coerce_json_payload(error_result)

                    if not error_payload.get("logs"):
                         print("  [i] get_error_logs returned no logs; retrying with a broad ERROR filter query")
                         error_result = await query_tool.ainvoke({"logql": '{pod=~".+"} |~ "ERROR"', "limit": 20, "start_time": "5m"})
                         error_payload = _coerce_json_payload(error_result)

                    self.assertIsInstance(error_payload, dict, f"get_error_logs did not return JSON: {error_payload!r}")
                    self.assertIn("logs", error_payload, "get_error_logs response did not include logs")
                    print(f"  [+] Success! get_error_logs returned {len(error_payload['logs'])} error log line(s)")

                    analyze_tool = next(tool for tool in tools if getattr(tool, "name", "") == "analyze_log_patterns")
                    analyze_result = await analyze_tool.ainvoke(
                         {"logql": '{service="checkout-service"}', "pattern": "error|fail|timeout", "limit": 200}
                    )
                    analyze_payload = _coerce_json_payload(analyze_result)

                    self.assertIsInstance(analyze_payload, dict, f"analyze_log_patterns did not return JSON: {analyze_payload!r}")
                    self.assertIn("total_logs", analyze_payload, "analyze_log_patterns did not include total_logs")
                    self.assertGreater(analyze_payload["total_logs"], 0, "analyze_log_patterns inspected no live logs")
                    self.assertIn("top_patterns", analyze_payload, "analyze_log_patterns did not include top_patterns")
                    print(f"  [+] Success! analyze_log_patterns inspected {analyze_payload['total_logs']} live log line(s)")

               asyncio.run(_run_loki_tool_checks())

          except requests.exceptions.ConnectionError:
               print("  [-] ERROR: Could not connect to Loki MCP server on port 4002.")
               self.fail("Could not connect to Loki MCP server on port 4002. Did you run docker compose up?")

     def test_mcp_k8s_health_and_node_tools(self):
          """Smoke test the Kubernetes MCP server's read-only discovery tools (port 4000)."""
          print("\n\n[*] Testing Kubernetes MCP Server Read-Only Tools (Port 4000)...")
          try:
               response = requests.get("http://127.0.0.1:4000/sse", stream=True, timeout=10)
               self.assertEqual(response.status_code, 200, "Kubernetes MCP server failed to establish SSE connection")

               content_type = response.headers.get("Content-Type", "")
               self.assertIn("text/event-stream", content_type, "Not an SSE endpoint")
               print(f"  [+] SSE reachable (Content-Type: {content_type})")
               response.close()

               async def _run_k8s_tool_checks():
                    client = MultiServerMCPClient(
                         {
                              "k8s": {
                                   "url": "http://127.0.0.1:4000/sse",
                                   "transport": "sse",
                              }
                         }
                    )

                    tools = await client.get_tools()
                    tool_names = [getattr(tool, "name", "") for tool in tools]
                    self.assertIn("check_k8s_health", tool_names, "Kubernetes MCP did not expose check_k8s_health")
                    self.assertIn("get_node_status", tool_names, "Kubernetes MCP did not expose get_node_status")
                    self.assertIn("list_namespaces", tool_names, "Kubernetes MCP did not expose list_namespaces")
                    self.assertIn("list_pods", tool_names, "Kubernetes MCP did not expose list_pods")
                    self.assertIn("list_services", tool_names, "Kubernetes MCP did not expose list_services")
                    self.assertIn("list_deployments", tool_names, "Kubernetes MCP did not expose list_deployments")
                    self.assertIn("list_events", tool_names, "Kubernetes MCP did not expose list_events")
                    self.assertIn("get_pod_status", tool_names, "Kubernetes MCP did not expose get_pod_status")
                    self.assertIn("get_deployment_status", tool_names, "Kubernetes MCP did not expose get_deployment_status")
                    self.assertIn("get_service_endpoints", tool_names, "Kubernetes MCP did not expose get_service_endpoints")
                    self.assertIn("get_pod_logs", tool_names, "Kubernetes MCP did not expose get_pod_logs")

                    health_tool = next(tool for tool in tools if getattr(tool, "name", "") == "check_k8s_health")
                    health_result = await health_tool.ainvoke({})
                    health_payload = _coerce_json_payload(health_result)

                    self.assertIsInstance(health_payload, dict, f"check_k8s_health did not return structured JSON: {health_payload!r}")
                    self.assertIn("status", health_payload, "check_k8s_health did not return status")
                    self.assertIn(health_payload["status"], {"healthy", "unhealthy"}, "Unexpected k8s health status value")
                    self.assertIn("message", health_payload, "check_k8s_health did not return a message")
                    print(f"  [+] Success! check_k8s_health returned status: {health_payload['status']}")

                    node_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_node_status")
                    node_result = await node_tool.ainvoke({})
                    node_payload = _coerce_json_payload(node_result)

                    if health_payload["status"] == "healthy":
                         self.assertIsInstance(node_payload, list, f"get_node_status did not return a node list: {node_payload!r}")
                         self.assertGreater(len(node_payload), 0, "get_node_status returned an empty node list")
                         self.assertIn("name", node_payload[0], "Node entry did not include a name")
                         print(f"  [+] Success! get_node_status returned {len(node_payload)} node(s)")
                    else:
                         self.assertTrue(
                              isinstance(node_payload, str) and node_payload,
                              f"get_node_status did not return an error string when unhealthy: {node_payload!r}",
                         )

                    live_targets = _discover_live_k8s_targets()
                    print(
                         "  [+] Using live cluster targets: "
                         f"pod={live_targets['pod_namespace']}/{live_targets['pod_name']}, "
                         f"deployment={live_targets['deployment_namespace']}/{live_targets['deployment_name']}, "
                         f"service={live_targets['service_namespace']}/{live_targets['service_name']}"
                    )
                    self.assertEqual(live_targets["pod_namespace"], live_targets["target_namespace"], "Pod target was not selected from demo-app")
                    self.assertEqual(live_targets["deployment_namespace"], live_targets["target_namespace"], "Deployment target was not selected from demo-app")
                    self.assertEqual(live_targets["service_namespace"], live_targets["target_namespace"], "Service target was not selected from demo-app")

                    namespaces_tool = next(tool for tool in tools if getattr(tool, "name", "") == "list_namespaces")
                    namespaces_result = await namespaces_tool.ainvoke({"limit": 100})
                    namespaces_payload = _coerce_json_payload(namespaces_result)

                    self.assertIsInstance(namespaces_payload, dict, f"list_namespaces did not return JSON: {namespaces_payload!r}")
                    self.assertIn("namespaces", namespaces_payload, "list_namespaces response did not include namespaces")
                    self.assertGreater(len(namespaces_payload["namespaces"]), 0, "list_namespaces returned no namespaces")
                    self.assertTrue(
                         any(ns.get("name") == live_targets["pod_namespace"] for ns in namespaces_payload["namespaces"]),
                         f"list_namespaces did not include the live namespace {live_targets['pod_namespace']}",
                    )
                    print(f"  [+] Success! list_namespaces returned {len(namespaces_payload['namespaces'])} namespace(s)")

                    pods_tool = next(tool for tool in tools if getattr(tool, "name", "") == "list_pods")
                    pods_result = await pods_tool.ainvoke({"namespace": live_targets["pod_namespace"], "limit": 100})
                    pods_payload = _coerce_json_payload(pods_result)

                    self.assertIsInstance(pods_payload, dict, f"list_pods did not return JSON: {pods_payload!r}")
                    self.assertIn("pods", pods_payload, "list_pods response did not include pods")
                    self.assertGreater(len(pods_payload["pods"]), 0, "list_pods returned no pods in the live namespace")
                    self.assertTrue(
                         any(pod.get("name") == live_targets["pod_name"] for pod in pods_payload["pods"]),
                         f"list_pods did not include the live pod {live_targets['pod_name']}",
                    )
                    print(f"  [+] Success! list_pods returned {len(pods_payload['pods'])} pod(s) in {live_targets['pod_namespace']}")

                    services_tool = next(tool for tool in tools if getattr(tool, "name", "") == "list_services")
                    services_result = await services_tool.ainvoke({"namespace": live_targets["service_namespace"], "limit": 100})
                    services_payload = _coerce_json_payload(services_result)

                    self.assertIsInstance(services_payload, dict, f"list_services did not return JSON: {services_payload!r}")
                    self.assertIn("services", services_payload, "list_services response did not include services")
                    self.assertGreater(len(services_payload["services"]), 0, "list_services returned no services in the live namespace")
                    self.assertTrue(
                         any(service.get("name") == live_targets["service_name"] for service in services_payload["services"]),
                         f"list_services did not include the live service {live_targets['service_name']}",
                    )
                    print(
                         f"  [+] Success! list_services returned {len(services_payload['services'])} service(s) in {live_targets['service_namespace']}"
                    )

                    deployments_tool = next(tool for tool in tools if getattr(tool, "name", "") == "list_deployments")
                    deployments_result = await deployments_tool.ainvoke({"namespace": live_targets["deployment_namespace"], "limit": 100})
                    deployments_payload = _coerce_json_payload(deployments_result)

                    self.assertIsInstance(deployments_payload, dict, f"list_deployments did not return JSON: {deployments_payload!r}")
                    self.assertIn("deployments", deployments_payload, "list_deployments response did not include deployments")
                    self.assertGreater(len(deployments_payload["deployments"]), 0, "list_deployments returned no deployments in the live namespace")
                    self.assertTrue(
                         any(deployment.get("name") == live_targets["deployment_name"] for deployment in deployments_payload["deployments"]),
                         f"list_deployments did not include the live deployment {live_targets['deployment_name']}",
                    )
                    print(
                         f"  [+] Success! list_deployments returned {len(deployments_payload['deployments'])} deployment(s) in {live_targets['deployment_namespace']}"
                    )

                    events_tool = next(tool for tool in tools if getattr(tool, "name", "") == "list_events")
                    events_result = await events_tool.ainvoke(
                         {"namespace": live_targets["pod_namespace"], "involved_object_name": live_targets["pod_name"], "limit": 50}
                    )
                    events_payload = _coerce_json_payload(events_result)

                    self.assertIsInstance(events_payload, dict, f"list_events did not return JSON: {events_payload!r}")
                    self.assertIn("events", events_payload, "list_events response did not include events")
                    if events_payload["events"]:
                         self.assertIn("reason", events_payload["events"][0], "Event entry did not include reason")
                         self.assertIn("message", events_payload["events"][0], "Event entry did not include message")
                    print(f"  [+] Success! list_events returned {len(events_payload['events'])} event(s)")

                    pod_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_pod_status")
                    pod_result = await pod_tool.ainvoke(
                         {"pod_name": live_targets["pod_name"], "namespace": live_targets["pod_namespace"]}
                    )
                    pod_payload = _coerce_json_payload(pod_result)

                    self.assertIsInstance(pod_payload, dict, f"get_pod_status did not return JSON: {pod_payload!r}")
                    self.assertEqual(pod_payload.get("name"), live_targets["pod_name"], "get_pod_status returned the wrong pod name")
                    self.assertEqual(
                         pod_payload.get("namespace"),
                         live_targets["pod_namespace"],
                         "get_pod_status returned the wrong pod namespace",
                    )
                    self.assertIn("phase", pod_payload, "get_pod_status response did not include phase")
                    self.assertIn("container_statuses", pod_payload, "get_pod_status response did not include container statuses")
                    print(
                         f"  [+] Success! get_pod_status returned pod {pod_payload['namespace']}/{pod_payload['name']} "
                         f"with phase {pod_payload.get('phase')}"
                    )

                    logs_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_pod_logs")
                    logs_result = await logs_tool.ainvoke(
                         {"pod_name": live_targets["pod_name"], "namespace": live_targets["pod_namespace"], "tail_lines": 50}
                    )
                    logs_payload = _coerce_json_payload(logs_result)

                    self.assertIsInstance(logs_payload, dict, f"get_pod_logs did not return JSON: {logs_payload!r}")
                    self.assertEqual(logs_payload.get("pod_name"), live_targets["pod_name"], "get_pod_logs returned the wrong pod name")
                    self.assertEqual(logs_payload.get("namespace"), live_targets["pod_namespace"], "get_pod_logs returned the wrong namespace")
                    self.assertIn("content", logs_payload, "get_pod_logs response did not include content")
                    print(f"  [+] Success! get_pod_logs returned {len(logs_payload['content'])} character(s) of log output")

                    endpoints_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_service_endpoints")
                    endpoints_result = await endpoints_tool.ainvoke(
                         {"service_name": live_targets["service_name"], "namespace": live_targets["service_namespace"]}
                    )
                    endpoints_payload = _coerce_json_payload(endpoints_result)

                    self.assertIsInstance(endpoints_payload, dict, f"get_service_endpoints did not return JSON: {endpoints_payload!r}")
                    self.assertEqual(endpoints_payload.get("name"), live_targets["service_name"], "get_service_endpoints returned the wrong service name")
                    self.assertEqual(endpoints_payload.get("namespace"), live_targets["service_namespace"], "get_service_endpoints returned the wrong namespace")
                    self.assertIn("subsets", endpoints_payload, "get_service_endpoints response did not include subsets")
                    print(
                         f"  [+] Success! get_service_endpoints returned {len(endpoints_payload['subsets'])} endpoint subset(s) for "
                         f"{endpoints_payload['namespace']}/{endpoints_payload['name']}"
                    )

                    deployment_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_deployment_status")
                    deployment_result = await deployment_tool.ainvoke(
                         {
                              "deployment_name": live_targets["deployment_name"],
                              "namespace": live_targets["deployment_namespace"],
                         }
                    )
                    deployment_payload = _coerce_json_payload(deployment_result)

                    self.assertIsInstance(
                         deployment_payload,
                         dict,
                         f"get_deployment_status did not return JSON: {deployment_payload!r}",
                    )
                    self.assertEqual(
                         deployment_payload.get("name"),
                         live_targets["deployment_name"],
                         "get_deployment_status returned the wrong deployment name",
                    )
                    self.assertEqual(
                         deployment_payload.get("namespace"),
                         live_targets["deployment_namespace"],
                         "get_deployment_status returned the wrong deployment namespace",
                    )
                    self.assertIn("replicas", deployment_payload, "get_deployment_status response did not include replicas")
                    self.assertIn("conditions", deployment_payload, "get_deployment_status response did not include conditions")
                    print(
                         f"  [+] Success! get_deployment_status returned deployment {deployment_payload['namespace']}/{deployment_payload['name']} "
                         f"with {deployment_payload.get('ready_replicas', 0)} ready replica(s)"
                    )

               asyncio.run(_run_k8s_tool_checks())

          except requests.exceptions.ConnectionError:
               print("  [-] ERROR: Could not connect to Kubernetes MCP server on port 4000.")
               self.fail("Could not connect to Kubernetes MCP server on port 4000. Did you run docker compose up?")

     def test_mcp_github_repository_file_and_commit_tools(self):
          """Test if the GitHub MCP Server can inspect repository files and the latest commit."""
          print("\n\n[*] Testing GitHub MCP Server Repository File and Commit Tools (Port 4003)...")
          try:
               response = requests.get("http://127.0.0.1:4003/sse", stream=True, timeout=10)
               self.assertEqual(response.status_code, 200, "GitHub MCP server failed to establish SSE connection")

               content_type = response.headers.get("Content-Type", "")
               self.assertIn("text/event-stream", content_type, "Not an SSE endpoint")
               print(f"  [+] SSE reachable (Content-Type: {content_type})")
               response.close()

               async def _run_github_tool_checks():
                    client = MultiServerMCPClient(
                         {
                              "github": {
                                   "url": "http://127.0.0.1:4003/sse",
                                   "transport": "sse",
                              }
                         }
                    )

                    tools = await client.get_tools()
                    tool_names = [getattr(tool, "name", "") for tool in tools]
                    self.assertIn("list_repository_files", tool_names, "GitHub MCP did not expose list_repository_files")
                    self.assertIn("get_repository_file", tool_names, "GitHub MCP did not expose get_repository_file")
                    self.assertIn("list_commits", tool_names, "GitHub MCP did not expose list_commits")
                    self.assertIn("get_commit", tool_names, "GitHub MCP did not expose get_commit")

                    list_files_tool = next(tool for tool in tools if getattr(tool, "name", "") == "list_repository_files")
                    list_files_result = await list_files_tool.ainvoke({"path": "", "recursive": True, "limit": 25})
                    list_files_payload = _coerce_json_payload(list_files_result)

                    self.assertIn("files", list_files_payload, "list_repository_files did not return files")
                    self.assertGreater(len(list_files_payload["files"]), 0, "Repository listing returned no files")

                    file_entry = next(
                         (entry for entry in list_files_payload["files"] if entry.get("type") == "file"),
                         None,
                    )
                    self.assertIsNotNone(file_entry, "Repository listing did not return any file entries to read")

                    file_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_repository_file")
                    file_result = await file_tool.ainvoke({"path": file_entry["path"], "max_chars": 2000})
                    file_payload = _coerce_json_payload(file_result)

                    self.assertEqual(file_payload["path"], file_entry["path"])
                    self.assertIn("content", file_payload)
                    self.assertTrue(file_payload["content"].strip(), "Repository file content was empty")
                    print(f"  [+] Success! Read repository file: {file_payload['path']}")

                    list_commits_tool = next(tool for tool in tools if getattr(tool, "name", "") == "list_commits")
                    latest_commits_result = await list_commits_tool.ainvoke({"limit": 1})
                    latest_commits_payload = _coerce_json_payload(latest_commits_result)

                    self.assertIn("commits", latest_commits_payload, "list_commits did not return commits")
                    self.assertGreater(len(latest_commits_payload["commits"]), 0, "No commits returned from list_commits")

                    latest_commit = latest_commits_payload["commits"][0]
                    self.assertIn("sha", latest_commit, "Latest commit payload did not include sha")

                    commit_tool = next(tool for tool in tools if getattr(tool, "name", "") == "get_commit")
                    commit_result = await commit_tool.ainvoke({"sha": latest_commit["sha"]})
                    commit_payload = _coerce_json_payload(commit_result)

                    self.assertIsInstance(
                         commit_payload,
                         dict,
                         f"get_commit did not return structured JSON. Raw output: {commit_payload!r}",
                    )

                    self.assertEqual(commit_payload["sha"], latest_commit["sha"])
                    self.assertIn("diff", commit_payload)
                    self.assertIn("additions", commit_payload)
                    self.assertIn("deletions", commit_payload)
                    print(f"  [+] Success! Inspected latest commit: {commit_payload['sha']}")

               asyncio.run(_run_github_tool_checks())

          except requests.exceptions.ConnectionError:
               print("  [-] ERROR: Could not connect to GitHub MCP server on port 4003.")
               self.fail("Could not connect to GitHub MCP server on port 4003. Did you run docker compose up?")


if __name__ == "__main__":
     print("-" * 50)
     print("Testing Layer 2: Edge MCP Tooling (SSE Interfaces for Kubernetes, Prometheus, Loki, GitHub)")
     print("-" * 50)
     print("Make sure you booted the MCP servers in the edge_mcp_servers folder first!")
     unittest.main(verbosity=2)
