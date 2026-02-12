"""DOM Density Map — Text-based page layout maps for LLM browser automation."""

__version__ = "0.1.0"

from .cdp import CDP, get_ws_url, is_chrome_running
from .core import render_density_map, render_sparse_map, render_elements_at

__all__ = [
    "CDP",
    "get_ws_url",
    "is_chrome_running",
    "render_density_map",
    "render_sparse_map",
    "render_elements_at",
]
