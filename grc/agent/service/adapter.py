"""Compatibility import for the MainAgent-owned service architecture."""

from .mainagent_service import ServiceAgent, build_service_agent

__all__ = ["ServiceAgent", "build_service_agent"]
