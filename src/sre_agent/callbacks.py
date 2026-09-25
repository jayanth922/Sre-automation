import logging
from typing import Any, Dict, List, Optional, Union
from uuid import UUID
from datetime import datetime, timezone

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from .redis_state_store import get_state_store

logger = logging.getLogger(__name__)

class RedisLogCallbackHandler(BaseCallbackHandler):
    """
    Callback handler that writes tool inputs/outputs and LLM thoughts to Redis logs.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.state_store = get_state_store()

    def _log(self, message: str):
        """Helper to log with timestamp."""
        timestamp = datetime.now(timezone.utc).isoformat()
        self.state_store.append_log(self.session_id, f"[{timestamp}] {message}")

    def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> Any:
        """Run when LLM starts running."""
        self._log("🧠 Thinking...")

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> Any:
        """Run when LLM ends running."""
        # Extract the text generation
        if response.generations and response.generations[0]:
            text = response.generations[0][0].text
            # Truncate for readability in logs
            if len(text) > 200:
                text = text[:200] + "..."
            self._log(f"💡 Thought: {text}")

    def on_tool_start(
        self, serialized: Dict[str, Any], input_str: str, **kwargs: Any
    ) -> Any:
        """Run when tool starts running."""
        tool_name = serialized.get("name")
        msg = f"🔧 Calling tool: {tool_name}"
        # Truncate input if too long
        if len(input_str) > 200:
             msg += f" with args: {input_str[:200]}..."
        else:
             msg += f" with args: {input_str}"
        self._log(msg)

    def on_tool_end(self, output: str, **kwargs: Any) -> Any:
        """Run when tool ends running."""
        # Tool output can be large, so we truncate
        out_str = str(output)
        if len(out_str) > 200:
            out_str = out_str[:200] + "..."
        msg = f"✅ Tool output: {out_str}"
        self._log(msg)

    def on_tool_error(
        self, error: Union[Exception, KeyboardInterrupt], **kwargs: Any
    ) -> Any:
        """Run when tool errors."""
        msg = f"❌ Tool error: {error}"
        self._log(msg)

    def on_agent_action(self, action: Any, **kwargs: Any) -> Any:
        """Run on agent action."""
        # Usually redundant if we track tool start, but useful for reasoning
        log = f"🤖 Agent Action: {action.tool}"
        self._log(log)
        
    def on_chain_start(
        self, serialized: Dict[str, Any], inputs: Dict[str, Any], **kwargs: Any
    ) -> Any:
        """Run when chain starts running."""
        pass
