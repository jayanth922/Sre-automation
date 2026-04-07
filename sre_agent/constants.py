#!/usr/bin/env python3

import os
import logging

from pydantic import BaseModel, Field

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


class ModelConfig(BaseModel):
    """Model configuration constants."""

    # Groq model IDs
    groq_model_id: str = Field(
        default="llama-3.1-8b-instant",
        description="Default Groq model ID",
    )

    # Model parameters
    default_temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        description="Default temperature for LLM generation",
    )

    default_max_tokens: int = Field(
        default=4096,
        ge=1,
        le=100000,
        description="Default max tokens for agent responses",
    )

    output_formatter_max_tokens: int = Field(
        default=1000,
        ge=1,
        le=100000,
        description="Max tokens for output formatter LLM calls",
    )

    # Ollama settings
    ollama_base_url: str = Field(
        default=os.getenv("OLLAMA_BASE_URL", "http://ollama:11434"),
        description="Base URL for Ollama service",
    )
    ollama_model: str = Field(
        default=os.getenv("OLLAMA_MODEL", "gemma3:1b"),
        description="Ollama model to use",
    )
    ollama_num_ctx: int = Field(
        default=int(os.getenv("OLLAMA_NUM_CTX", "32768")),
        ge=1,
        le=131072,
        description="Ollama context window size",
    )

    # Gemini settings
    gemini_model: str = Field(
        default="gemini-3.1-flash-lite-preview",
        description="Default Gemini model ID",
    )


class TimeoutConfig(BaseModel):
    """Timeout configuration constants."""

    graph_execution_timeout_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="Maximum time to wait for graph execution (10 minutes)",
    )

    mcp_tools_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Maximum time to wait for MCP tools loading",
    )


class PromptConfig(BaseModel):
    """Prompt configuration constants."""

    prompts_directory: str = Field(
        default="config/prompts",
        description="Directory containing prompt template files",
    )

    agent_prompt_files: dict[str, str] = Field(
        default={
            "kubernetes": "kubernetes_agent_prompt.txt",
            "logs": "logs_agent_prompt.txt",
            "metrics": "metrics_agent_prompt.txt",
            "runbooks": "runbooks_agent_prompt.txt",
        },
        description="Mapping of agent types to their prompt files",
    )

    supervisor_prompt_files: dict[str, str] = Field(
        default={
            "plan_aggregation": "supervisor_plan_aggregation.txt",
            "standard_aggregation": "supervisor_standard_aggregation.txt",
            "system": "supervisor_aggregation_system.txt",
        },
        description="Supervisor aggregation prompt files",
    )

    output_formatter_prompt_files: dict[str, str] = Field(
        default={
            "executive_summary_system": "executive_summary_system.txt",
            "executive_summary_user_template": "executive_summary_user_template.txt",
        },
        description="Output formatter prompt files",
    )

    base_prompt_file: str = Field(
        default="agent_base_prompt.txt",
        description="Base prompt template used by all agents",
    )

    enable_prompt_caching: bool = Field(
        default=True, description="Whether to enable LRU caching for prompt loading"
    )

    max_cache_size: int = Field(
        default=32,
        ge=1,
        le=128,
        description="Maximum number of prompts to cache in memory",
    )


class ApplicationConfig(BaseModel):
    """Application configuration constants."""

    agent_model_name: str = Field(
        default="sre-multi-agent", description="Model name returned in API responses"
    )

    default_output_dir: str = Field(
        default="./reports",
        description="Default directory for saving investigation reports",
    )

    conversation_state_file: str = Field(
        default=".multi_agent_conversation_state.json",
        description="Filename for saving conversation state",
    )

    spinner_chars: list[str] = Field(
        default=["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
        description="Characters used for spinner animation",
    )


class AgentMetadata(BaseModel):
    """Metadata for a single agent."""

    actor_id: str = Field(description="Unique actor ID for memory operations")
    display_name: str = Field(description="Human-readable agent name")
    description: str = Field(description="Agent capabilities description")
    agent_type: str = Field(description="Agent type for prompt loading")


class AgentsConstant(BaseModel):
    """Agent-specific constants for the SRE system."""

    default_actor_id: str = Field(
        default="sre-agent",
        description="Default actor ID used for saving and retrieving memories",
    )

    default_user_id: str = Field(
        default="default-sre-user",
        description="Default user ID for memory operations when no user is specified",
    )

    session_prefix: str = Field(
        default="sre-session", description="Prefix used for session IDs"
    )

    memory_types: dict[str, str] = Field(
        default={
            "preferences": "preferences",
            "infrastructure": "infrastructure",
            "investigations": "investigations",
        },
        description="Memory type identifiers",
    )

    # Agent metadata for consistent identity management
    agents: dict[str, AgentMetadata] = Field(
        default={
            "kubernetes": AgentMetadata(
                actor_id="kubernetes-agent",
                display_name="Kubernetes Infrastructure Agent",
                description="Manages Kubernetes cluster operations and monitoring",
                agent_type="kubernetes",
            ),
            "logs": AgentMetadata(
                actor_id="logs-agent",
                display_name="Application Logs Agent",
                description="Handles application log analysis and searching",
                agent_type="logs",
            ),
            "metrics": AgentMetadata(
                actor_id="metrics-agent",
                display_name="Performance Metrics Agent",
                description="Provides application performance and resource metrics",
                agent_type="metrics",
            ),
            "runbooks": AgentMetadata(
                actor_id="runbooks-agent",
                display_name="Operational Runbooks Agent",
                description="Provides operational procedures and troubleshooting guides",
                agent_type="runbooks",
            ),
            "github": AgentMetadata(
                actor_id="github-agent",
                display_name="Code Change Intelligence Agent",
                description="Correlates code changes (commits, PRs) with incidents and identifies bad commits",
                agent_type="github",
            ),
            "supervisor": AgentMetadata(
                actor_id="supervisor-agent",
                display_name="Supervisor Agent",
                description="Orchestrates investigation planning and coordinates multiple specialized agents",
                agent_type="supervisor",
            ),
        },
        description="Metadata for all agents in the system",
    )


class SREConstants:
    """Central constants configuration for the SRE Agent system.

    This class provides a centralized way to access all configuration constants
    used throughout the SRE Agent application. It uses Pydantic models for
    validation and type safety.

    Usage:
        from .constants import SREConstants

        # Access model configuration
        model_id = SREConstants.model.groq_model_id
        temperature = SREConstants.model.default_temperature

        # Access timeout configuration
        timeout = SREConstants.timeouts.graph_execution_timeout_seconds

        # Access prompt configuration
        prompts_dir = SREConstants.prompts.prompts_directory
        agent_files = SREConstants.prompts.agent_prompt_files

        # Access application configuration
        output_dir = SREConstants.app.default_output_dir
    """

    model: ModelConfig = ModelConfig()
    timeouts: TimeoutConfig = TimeoutConfig()
    prompts: PromptConfig = PromptConfig()
    app: ApplicationConfig = ApplicationConfig()
    agents: AgentsConstant = AgentsConstant()

    @classmethod
    def get_model_config(cls, provider: str, **kwargs) -> dict:
        """Get model configuration for a specific provider.

        Args:
            provider: LLM provider
            **kwargs: Additional configuration overrides

        Returns:
            Dictionary with model configuration
        """
        if provider == "ollama":
            return {
                "model_id": kwargs.get("model_id", cls.model.ollama_model),
                "base_url": kwargs.get("base_url", cls.model.ollama_base_url),
                "temperature": kwargs.get("temperature", cls.model.default_temperature),
                "num_ctx": kwargs.get("num_ctx", cls.model.ollama_num_ctx),
            }
        
        if provider == "gemini":
            return {
                "model_id": kwargs.get("model_id", cls.model.gemini_model),
                "temperature": kwargs.get("temperature", cls.model.default_temperature),
            }
        
        if provider != "groq":
            raise ValueError(f"Unsupported provider: {provider}. Supported: 'groq', 'ollama', 'gemini'.")

        return {
            "model_id": kwargs.get("model_id", cls.model.groq_model_id),
            "max_tokens": kwargs.get("max_tokens", cls.model.default_max_tokens),
            "temperature": kwargs.get("temperature", cls.model.default_temperature),
        }

    @classmethod
    def get_output_formatter_config(cls, provider: str, **kwargs) -> dict:
        """Get model configuration for output formatter.

        Args:
            provider: LLM provider (only "groq" is supported)
            **kwargs: Additional configuration overrides

        Returns:
            Dictionary with output formatter model configuration
        """
        config = cls.get_model_config(provider, **kwargs)
        # Override max_tokens for output formatter
        config["max_tokens"] = kwargs.get(
            "max_tokens", cls.model.output_formatter_max_tokens
        )
        return config

    @classmethod
    def get_prompt_config(cls) -> PromptConfig:
        """Get prompt configuration.

        Returns:
            PromptConfig instance with all prompt settings
        """
        return cls.prompts


# Convenience instance for easy access
constants = SREConstants()

# Legacy support - individual constants for backward compatibility if needed
GROQ_MODEL_ID = constants.model.groq_model_id
DEFAULT_TEMPERATURE = constants.model.default_temperature
DEFAULT_MAX_TOKENS = constants.model.default_max_tokens
GRAPH_EXECUTION_TIMEOUT_SECONDS = constants.timeouts.graph_execution_timeout_seconds
AGENT_MODEL_NAME = constants.app.agent_model_name
DEFAULT_OUTPUT_DIR = constants.app.default_output_dir
DEFAULT_ACTOR_ID = constants.agents.default_actor_id
