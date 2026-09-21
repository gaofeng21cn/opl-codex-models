"""Error types mirroring CoreError / AppError from the original project."""


class CodexModelError(Exception):
    """Base error for the model manager."""


class ConfigurationNotFound(CodexModelError):
    def __init__(self, path):
        self.path = path
        super().__init__(f"尚未找到本机配置：{path}")


class InvalidConfiguration(CodexModelError):
    pass


class RuntimeNotFound(CodexModelError):
    pass


class InvalidCatalog(CodexModelError):
    pass


class ProcessFailed(CodexModelError):
    pass


class SetupFailed(CodexModelError):
    pass


class ConflictError(CodexModelError):
    """A destructive change was refused because state moved under us.

    Raised when an import/apply would silently overwrite or drop content that the
    user did not explicitly approve (a diverged pending catalog, a changed active
    catalog, or unconfirmed model removals). Callers surface the message and must
    not force the write on the user's behalf.
    """
