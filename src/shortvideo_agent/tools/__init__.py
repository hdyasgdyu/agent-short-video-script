"""工具包：导入即完成注册。"""

from .registry import (  # noqa: F401
    PHASE_TOOLS,
    TOOL_REGISTRY,
    describe_tools,
    execute_tool,
    tool_schemas,
)
from . import search_materials  # noqa: F401,E402
from . import duration  # noqa: F401,E402
from . import rules  # noqa: F401,E402
from . import script_tools  # noqa: F401,E402
