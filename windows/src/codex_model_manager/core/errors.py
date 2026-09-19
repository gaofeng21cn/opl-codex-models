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