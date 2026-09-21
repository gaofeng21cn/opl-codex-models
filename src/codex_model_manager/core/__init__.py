"""CodexModelManager core layer.

A Windows port of the core logic from gaofeng21cn/opl-codex-models
(original commit bb588846b28beeece759adcdc917e643fdb8077c, Apache-2.0).
The merge / override / custom-model rules mirror the Swift sources in
Sources/CodexModelCore and Sources/CodexModelManager.
"""

from .errors import (
    CodexModelError,
    InvalidCatalog,
    InvalidConfiguration,
    ProcessFailed,
    ConfigurationNotFound,
    RuntimeNotFound,
)

__all__ = [
    "CodexModelError",
    "InvalidCatalog",
    "InvalidConfiguration",
    "ProcessFailed",
    "ConfigurationNotFound",
    "RuntimeNotFound",
]