#!/usr/bin/env python3
"""
Real Notion MCP Server (Native FastMCP)

This MCP server directly uses the Notion API to access runbooks.
Uses standard mcp.server.fastmcp implementation.
"""

import json
import logging
import os
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP
from notion_client import Client
from notion_client.errors import APIResponseError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize Notion client
notion_client = None
notion_database_id = None
last_connection_attempt = 0
CONNECTION_RETRY_INTERVAL = 10 


def get_notion_client() -> Optional[Client]:
    """
    Get Notion client, attempting to initialize if necessary.
    Implements lazy loading and backoff.
    """
    global notion_client, notion_database_id, last_connection_attempt
    
    if notion_client:
        return notion_client

    # Check retry interval
    now = time.time()
    if now - last_connection_attempt < CONNECTION_RETRY_INTERVAL:
        return None
        
    last_connection_attempt = now

    notion_api_key = os.getenv("NOTION_API_KEY")
    notion_database_id_env = os.getenv("NOTION_DATABASE_ID")

    if not notion_api_key:
        logger.warning("⚠️ NOTION_API_KEY not set, server will not function")
        return None
 
    if not notion_database_id_env:
        logger.warning("⚠️ NOTION_DATABASE_ID not set, server will not function")
        return None

    try:
        logger.info("🔄 Attempting to connect to Notion...")
        client = Client(auth=notion_api_key)
        
        # Test connection by querying database
        client.databases.retrieve(database_id=notion_database_id_env)
        logger.info(f"✅ Connected to Notion database: {notion_database_id_env}")
        
        notion_client = client
        notion_database_id = notion_database_id_env
        return notion_client

    except Exception as e:
        logger.error(f"❌ Failed to initialize Notion client: {e}")
        return None


# Initialize FastMCP server
port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")

mcp = FastMCP("Notion Runbooks", host=host, port=port)


def _extract_text_from_block(block: dict) -> str:
    """Extract text content from a Notion block."""
    block_type = block.get("type", "")
    text_content = ""

    if block_type in ["paragraph", "heading_1", "heading_2", "heading_3"]:
        rich_text = block.get(block_type, {}).get("rich_text", [])
        text_content = "".join([item.get("plain_text", "") for item in rich_text])
    elif block_type == "bulleted_list_item":
        rich_text = block.get("bulleted_list_item", {}).get("rich_text", [])
        text_content = "• " + "".join([item.get("plain_text", "") for item in rich_text])
    elif block_type == "numbered_list_item":
        rich_text = block.get("numbered_list_item", {}).get("rich_text", [])
        text_content = "1. " + "".join([item.get("plain_text", "") for item in rich_text])
    elif block_type == "code":
        rich_text = block.get("code", {}).get("rich_text", [])
        code_text = "".join([item.get("plain_text", "") for item in rich_text])
        language = block.get("code", {}).get("language", "")
        text_content = f"```{language}\n{code_text}\n```"

    return text_content


@mcp.tool()
def check_notion_health() -> str:
    """
    Check the health of the Notion connection.
    Returns status and connectivity details.
    """
    client = get_notion_client()
    
    if client:
        return json.dumps({
            "status": "healthy",
            "message": "Connected to Notion API",
            "database_id": notion_database_id
        }, indent=2)
    else:
        # Determine why it failed
        reason = "Connection failed"
        if not os.getenv("NOTION_API_KEY"):
            reason = "Missing NOTION_API_KEY"
        elif not os.getenv("NOTION_DATABASE_ID"):
            reason = "Missing NOTION_DATABASE_ID"
            
        return json.dumps({
            "status": "unhealthy",
            "error": reason,
            "message": "Failed to connect to Notion. Check logs."
        }, indent=2)


@mcp.tool()
def search_runbooks(query: str) -> str:
    """
    Search runbooks in Notion database by querying page titles and content.
    
    Args:
        query: Search query to match against page titles and content
    
    Returns:
        JSON string with search results
    """
    client = get_notion_client()
    if not client:
        return json.dumps({"error": "Notion client not initialized. Check server logs."})

    logger.info(f"Searching runbooks: {query}")

    try:
        # Query Notion database
        # Search in title property (assuming database has a "Title" property)
        response = client.databases.query(
            database_id=notion_database_id,
            filter={
                "or": [
                    {
                        "property": "Title",
                        "title": {
                            "contains": query,
                        },
                    },
                    {
                        "property": "Name",  # Alternative property name
                        "title": {
                            "contains": query,
                        },
                    },
                ],
            },
        )

        results = []
        for page in response.get("results", []):
            page_id = page.get("id", "")
            page_properties = page.get("properties", {})

            # Extract title
            title = ""
            for prop_name, prop_data in page_properties.items():
                if prop_data.get("type") == "title":
                    title_rich_text = prop_data.get("title", [])
                    title = "".join([item.get("plain_text", "") for item in title_rich_text])
                    break

            results.append({
                "page_id": page_id,
                "title": title,
                "url": page.get("url", ""),
            })

        result = {
            "query": query,
            "results": results,
            "count": len(results),
        }

        return json.dumps(result, indent=2)

    except APIResponseError as e:
        logger.error(f"Notion API error: {e}")
        return json.dumps({"error": f"Notion search failed: {str(e)}"})
    except Exception as e:
        logger.error(f"Notion search error: {e}")
        return json.dumps({"error": f"Notion search failed: {str(e)}"})


@mcp.tool()
def get_runbook_content(page_id: str) -> str:
    """
    Get full content of a runbook page by Notion page ID.
    
    Args:
        page_id: Notion page ID
    
    Returns:
        JSON string with page content
    """
    client = get_notion_client()
    if not client:
        return json.dumps({"error": "Notion client not initialized. Check server logs."})

    logger.info(f"Getting runbook content: {page_id}")

    try:
        # Get page
        page = client.pages.retrieve(page_id=page_id)

        # Get page blocks
        blocks_response = client.blocks.children.list(block_id=page_id)

        # Extract content from blocks
        content_lines = []
        for block in blocks_response.get("results", []):
            text = _extract_text_from_block(block)
            if text:
                content_lines.append(text)

        # Get page properties
        page_properties = page.get("properties", {})
        title = ""
        for prop_name, prop_data in page_properties.items():
            if prop_data.get("type") == "title":
                title_rich_text = prop_data.get("title", [])
                title = "".join([item.get("plain_text", "") for item in title_rich_text])
                break

        result = {
            "page_id": page_id,
            "title": title,
            "url": page.get("url", ""),
            "content": "\n".join(content_lines),
        }

        return json.dumps(result, indent=2)

    except APIResponseError as e:
        logger.error(f"Notion API error: {e}")
        return json.dumps({"error": f"Failed to get runbook content: {str(e)}"})
    except Exception as e:
        logger.error(f"Notion get content error: {e}")
        return json.dumps({"error": f"Failed to get runbook content: {str(e)}"})


@mcp.tool()
def get_incident_playbook(incident_type: str) -> str:
    """
    Get incident playbook for a specific incident type.
    
    Args:
        incident_type: Type of incident (e.g., 'performance', 'availability', 'security')
    
    Returns:
        JSON string with playbook content
    """
    client = get_notion_client()
    if not client:
        return json.dumps({"error": "Notion client not initialized. Check server logs."})

    logger.info(f"Getting incident playbook: {incident_type}")

    # Search for playbook matching incident type
    search_results = search_runbooks(f"{incident_type} playbook")
    search_data = json.loads(search_results)

    if "error" in search_data:
        return search_results

    results = search_data.get("results", [])

    if not results:
        # Try alternative search
        search_results = search_runbooks(incident_type)
        search_data = json.loads(search_results)
        if "error" not in search_data:
            results = search_data.get("results", [])

    if results:
        # Get content of first match
        page_id = results[0].get("page_id")
        if page_id:
            return get_runbook_content(page_id)

    # Return empty result
    result = {
        "incident_type": incident_type,
        "message": "No playbook found for this incident type",
        "results": [],
    }

    return json.dumps(result, indent=2)


if __name__ == "__main__":
    logger.info(f"Starting Notion MCP Server on {host}:{port}")
    # Try initial connection
    get_notion_client()
    mcp.run(transport="sse")
